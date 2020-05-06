# -*- coding: utf-8 -*-
# Copyright 2007-2020 The HyperSpy developers
#
# This file is part of  HyperSpy.
#
#  HyperSpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
#  HyperSpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with  HyperSpy.  If not, see <http://www.gnu.org/licenses/>.


import logging
import types
import warnings
import inspect
import dask.array as da
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter, MaxNLocator

import hyperspy.misc.io.tools as io_tools
from hyperspy.exceptions import VisibleDeprecationWarning
from hyperspy.learn.mlpca import mlpca
from hyperspy.learn.ornmf import ornmf
from hyperspy.learn.orthomax import orthomax
from hyperspy.learn.rpca import orpca, rpca_godec
from hyperspy.learn.svd_pca import svd_pca
from hyperspy.learn.whitening import whiten_data
from hyperspy.misc.machine_learning import import_sklearn
from hyperspy.misc.utils import ordinal, stack
from hyperspy.external.progressbar import progressbar

try:
    import mdp

    mdp_installed = True
except ImportError:
    mdp_installed = False


_logger = logging.getLogger(__name__)


if import_sklearn.sklearn_installed:
    decomposition_algorithms = {
        "sklearn_pca": import_sklearn.sklearn.decomposition.PCA,
        "nmf": import_sklearn.sklearn.decomposition.NMF,
        "sparse_pca": import_sklearn.sklearn.decomposition.SparsePCA,
        "mini_batch_sparse_pca": import_sklearn.sklearn.decomposition.MiniBatchSparsePCA,
        "sklearn_fastica": import_sklearn.sklearn.decomposition.FastICA,
    }
    cluster_algorithms = {
        'kmeans': import_sklearn.sklearn.cluster.KMeans,
        'agglomerative': import_sklearn.sklearn.cluster.AgglomerativeClustering
    }



def _get_derivative(signal, diff_axes, diff_order):
    """Calculate the derivative of a signal."""
    if signal.axes_manager.signal_dimension == 1:
        signal = signal.diff(order=diff_order, axis=-1)
    else:
        # n-d signal case.
        # Compute the differences for each signal axis, unfold the
        # signal axes and stack the differences over the signal
        # axis.
        if diff_axes is None:
            diff_axes = signal.axes_manager.signal_axes
            iaxes = [axis.index_in_axes_manager for axis in diff_axes]
        else:
            iaxes = diff_axes
        diffs = [signal.derivative(order=diff_order, axis=i) for i in iaxes]
        for signal in diffs:
            signal.unfold()
        signal = stack(diffs, axis=-1)
        del diffs
    return signal


def _normalize_components(target, other, function=np.sum):
    """Normalize components according to a function."""
    coeff = function(target, axis=0)
    target /= coeff
    other *= coeff


class MVA:
    """Multivariate analysis capabilities for the Signal1D class."""

    def __init__(self):
        if not hasattr(self, "learning_results"):
            self.learning_results = LearningResults()

    def decomposition(
        self,
        normalize_poissonian_noise=False,
        algorithm="svd",
        output_dimension=None,
        centre=None,
        auto_transpose=True,
        navigation_mask=None,
        signal_mask=None,
        var_array=None,
        var_func=None,
        reproject=None,
        return_info=False,
        print_info=True,
        svd_solver="auto",
        copy=True,
        **kwargs,
    ):
        """Apply a decomposition to a dataset with a choice of algorithms.

        The results are stored in ``self.learning_results``.

        Read more in the :ref:`User Guide <mva.decomposition>`.

        Parameters
        ----------
        normalize_poissonian_noise : bool, default False
            If True, scale the signal to normalize Poissonian noise using
            the approach described in [Keenan2004]_.
        algorithm : {"svd", "mlpca", "sklearn_pca", "nmf", "sparse_pca", "mini_batch_sparse_pca", "rpca", "orpca", "ornmf", custom object}, default "svd"
            The decomposition algorithm to use. If algorithm is an object,
            it must implement a ``fit_transform()`` method or ``fit()`` and
            ``transform()`` methods, in the same manner as a scikit-learn estimator.
        output_dimension : None or int
            Number of components to keep/calculate.
            Default is None, i.e. ``min(data.shape)``.
        centre : {None, "navigation", "signal"}, default None
            * If None, the data is not centered prior to decomposition.
            * If "navigation", the data is centered along the navigation axis.
              Only used by the "svd" algorithm.
            * If "signal", the data is centered along the signal axis.
              Only used by the "svd" algorithm.
        auto_transpose : bool, default True
            If True, automatically transposes the data to boost performance.
            Only used by the "svd" algorithm.
        navigation_mask : boolean numpy array
            The navigation locations marked as True are not used in the
            decompostion.
        signal_mask : boolean numpy array
            The signal locations marked as True are not used in the
            decomposition.
        var_array : numpy array
            Array of variance for the maximum likelihood PCA algorithm.
            Only used by the "mlpca" algorithm.
        var_func : None or function or numpy array, default None
            * If None, ignored
            * If function, applies the function to the data to obtain ``var_array``.
              Only used by the "mlpca" algorithm.
            * If numpy array, creates ``var_array`` by applying a polynomial function
              defined by the array of coefficients to the data. Only used by
              the "mlpca" algorithm.
        reproject : {None, "signal", "navigation", "both"}, default None
            If not None, the results of the decomposition will be projected in
            the selected masked area.
        return_info: bool, default False
            The result of the decomposition is stored internally. However,
            some algorithms generate some extra information that is not
            stored. If True, return any extra information if available.
            In the case of sklearn.decomposition objects, this includes the
            sklearn Estimator object.
        print_info : bool, default True
            If True, print information about the decomposition being performed.
            In the case of sklearn.decomposition objects, this includes the
            values of all arguments of the chosen sklearn algorithm.
        svd_solver : {"auto", "full", "arpack", "randomized"}, default "auto"
            If auto:
                The solver is selected by a default policy based on `data.shape` and
                `output_dimension`: if the input data is larger than 500x500 and the
                number of components to extract is lower than 80% of the smallest
                dimension of the data, then the more efficient "randomized"
                method is enabled. Otherwise the exact full SVD is computed and
                optionally truncated afterwards.
            If full:
                run exact SVD, calling the standard LAPACK solver via
                :py:func:`scipy.linalg.svd`, and select the components by postprocessing
            If arpack:
                use truncated SVD, calling ARPACK solver via
                :py:func:`scipy.sparse.linalg.svds`. It requires strictly
                `0 < output_dimension < min(data.shape)`
            If randomized:
                use truncated SVD, calling :py:func:`sklearn.utils.extmath.randomized_svd`
                to estimate a limited number of components
        copy : bool, default True
            * If True, stores a copy of the data before any pre-treatments
              such as normalization in ``s._data_before_treatments``. The original
              data can then be restored by calling ``s.undo_treatments()``.
            * If False, no copy is made. This can be beneficial for memory
              usage, but care must be taken since data will be overwritten.
        **kwargs : extra keyword arguments
            Any keyword arguments are passed to the decomposition algorithm.

        Returns
        -------
        return_info : tuple(numpy array, numpy array) or sklearn.Estimator or None
            * If True and 'algorithm' in ['rpca', 'orpca', 'ornmf'], returns
              the low-rank (X) and sparse (E) matrices from robust PCA/NMF.
            * If True and 'algorithm' is an sklearn Estimator, returns the
              Estimator object.
            * Otherwise, returns None

        References
        ----------
        .. [Keenan2004] M. Keenan and P. Kotula, "Accounting for Poisson noise
            in the multivariate analysis of ToF-SIMS spectrum images", Surf.
            Interface Anal 36(3) (2004): 203-212.

        See Also
        --------
        * :py:meth:`~.signal.MVATools.plot_decomposition_factors`
        * :py:meth:`~.signal.MVATools.plot_decomposition_loadings`
        * :py:meth:`~.signal.MVATools.plot_decomposition_results`
        * :py:meth:`~.learn.mva.MVA.plot_explained_variance_ratio`
        * :py:meth:`~._signals.lazy.LazySignal.decomposition` for lazy signals

        """
        # Check data is suitable for decomposition
        if self.data.dtype.char not in np.typecodes["AllFloat"]:
            raise TypeError(
                "To perform a decomposition the data must be of the "
                f"float or complex type, but the current type is '{self.data.dtype}'. "
                "To fix this issue, you can change the type using the "
                "change_dtype method (e.g. s.change_dtype('float64')) "
                "and then repeat the decomposition.\n"
                "No decomposition was performed."
            )

        if self.axes_manager.navigation_size < 2:
            raise AttributeError(
                "It is not possible to decompose a dataset with navigation_size < 2"
            )

        # Check for deprecated algorithm arguments
        algorithms_deprecated = {
            "fast_svd": "svd",
            "fast_mlpca": "mlpca",
            "RPCA_GoDec": "rpca",
            "ORPCA": "orpca",
            "ORNMF": "ornmf",
        }
        new_algo = algorithms_deprecated.get(algorithm, None)
        if new_algo:
            if "fast" in algorithm:
                warnings.warn(
                    f"The algorithm name `{algorithm}` has been deprecated and will be "
                    f"removed in HyperSpy 2.0. Please use `{new_algo}` along with the "
                    "argument `svd_solver='randomized'` instead.",
                    VisibleDeprecationWarning,
                )
                svd_solver = "randomized"
            else:
                warnings.warn(
                    f"The algorithm name `{algorithm}` has been deprecated and will be "
                    f"removed in HyperSpy 2.0. Please use `{new_algo}` instead.",
                    VisibleDeprecationWarning,
                )

            # Update algorithm name
            algorithm = new_algo

        # Check algorithms requiring output_dimension
        algorithms_require_dimension = [
            "mlpca",
            "rpca",
            "orpca",
            "ornmf",
        ]
        if algorithm in algorithms_require_dimension and output_dimension is None:
            raise ValueError(f"`output_dimension` must be specified for '{algorithm}'")

        # Check sklearn-like algorithms
        is_sklearn_like = False
        algorithms_sklearn = [
            "sklearn_pca",
            "nmf",
            "sparse_pca",
            "mini_batch_sparse_pca",
        ]
        if algorithm in algorithms_sklearn:
            if not import_sklearn.sklearn_installed:
                raise ImportError(f"algorithm='{algorithm}' requires scikit-learn")

            # Initialize the sklearn estimator
            is_sklearn_like = True
            estim = decomposition_algorithms[algorithm](
                n_components=output_dimension, **kwargs
            )

        elif hasattr(algorithm, "fit_transform") or (
            hasattr(algorithm, "fit") and hasattr(algorithm, "transform")
        ):
            # Check properties of algorithm against typical sklearn objects
            # If algorithm is an object that implements the methods fit(),
            # transform() and fit_transform(), then we can use it like an
            # sklearn estimator. This also allows us to, for example, use
            # Pipeline and GridSearchCV objects.
            is_sklearn_like = True
            estim = algorithm

        # MLPCA is designed to handle count data & Poisson noise
        if algorithm == "mlpca" and normalize_poissonian_noise:
            warnings.warn(
                "It does not make sense to normalize Poisson noise with "
                "the maximum-likelihood MLPCA algorithm. Therefore, "
                "`normalize_poissonian_noise` is set to False.",
                UserWarning,
            )
            normalize_poissonian_noise = False

        # Check for deprecated polyfit
        polyfit = kwargs.get("polyfit", False)
        if polyfit:
            warnings.warn(
                "The `polyfit` argument has been deprecated and will be "
                "removed in HyperSpy 2.0. Please use `var_func` instead.",
                VisibleDeprecationWarning,
            )
            var_func = polyfit

        # Initialize return_info and print_info
        to_return = None
        to_print = [
            "Decomposition info:",
            f"  normalize_poissonian_noise={normalize_poissonian_noise}",
            f"  algorithm={algorithm}",
            f"  output_dimension={output_dimension}",
            f"  centre={centre}",
        ]

        # Backup the original data (on by default to
        # mimic previous behaviour)
        if copy:
            self._data_before_treatments = self.data.copy()

        # set the output target (peak results or not?)
        target = LearningResults()

        # Apply pre-treatments
        # Transform the data in a line spectrum
        self._unfolded4decomposition = self.unfold()
        try:
            _logger.info("Performing decomposition analysis")

            if hasattr(navigation_mask, "ravel"):
                navigation_mask = navigation_mask.ravel()

            if hasattr(signal_mask, "ravel"):
                signal_mask = signal_mask.ravel()

            # Normalize the poissonian noise
            # TODO this function can change the masks and
            # this can cause problems when reprojecting
            if normalize_poissonian_noise:
                if centre is not None:
                    raise ValueError(
                        "normalize_poissonian_noise=True is only compatible "
                        f"with `centre=None`, not `centre={centre}`."
                    )

                self.normalize_poissonian_noise(
                    navigation_mask=navigation_mask, signal_mask=signal_mask,
                )

            # The rest of the code assumes that the first data axis
            # is the navigation axis. We transpose the data if that
            # is not the case.
            if self.axes_manager[0].index_in_array == 0:
                dc = self.data
            else:
                dc = self.data.T

            # Transform the None masks in slices to get the right behaviour
            if navigation_mask is None:
                navigation_mask = slice(None)
            else:
                navigation_mask = ~navigation_mask
            if signal_mask is None:
                signal_mask = slice(None)
            else:
                signal_mask = ~signal_mask

            # WARNING: signal_mask and navigation_mask values are now their
            # negaties i.e. True -> False and viceversa. However, the
            # stored value (at the end of the method) coincides with the
            # input masks

            data_ = dc[:, signal_mask][navigation_mask, :]

            # Reset the explained_variance which is not set by all the
            # algorithms
            explained_variance = None
            explained_variance_ratio = None
            number_significant_components = None
            mean = None

            if algorithm == "svd":
                factors, loadings, explained_variance, mean = svd_pca(
                    data_,
                    svd_solver=svd_solver,
                    output_dimension=output_dimension,
                    centre=centre,
                    auto_transpose=auto_transpose,
                    **kwargs,
                )

            elif algorithm == "mlpca":
                if var_array is not None and var_func is not None:
                    raise ValueError(
                        "`var_func` and `var_array` cannot both be defined. "
                        "Please define just one of them."
                    )
                elif var_array is None and var_func is None:
                    _logger.info(
                        "No variance array provided. Assuming Poisson-distributed data"
                    )
                    var_array = data_
                elif var_array is not None:
                    if var_array.shape != data_.shape:
                        raise ValueError(
                            "`var_array` must have the same shape as input data"
                        )
                elif var_func is not None:
                    if callable(var_func):
                        var_array = var_func(data_)
                    elif isinstance(var_func, (np.ndarray, list)):
                        var_array = np.polyval(var_func, data_)
                    else:
                        raise ValueError(
                            "`var_func` must be either a function or an array "
                            "defining the coefficients of a polynomial"
                        )

                U, S, V, Sobj = mlpca(
                    data_, var_array, output_dimension, svd_solver=svd_solver, **kwargs,
                )

                loadings = U * S
                factors = V
                explained_variance = S ** 2 / len(factors)

            elif algorithm == "rpca":
                X, E, U, S, V = rpca_godec(data_, rank=output_dimension, **kwargs)

                loadings = U * S
                factors = V
                explained_variance = S ** 2 / len(factors)

                if return_info:
                    to_return = (X, E)

            elif algorithm == "orpca":
                if return_info:
                    X, E, U, S, V = orpca(
                        data_, rank=output_dimension, store_error=True, **kwargs
                    )

                    loadings = U * S
                    factors = V
                    explained_variance = S ** 2 / len(factors)

                    to_return = (X, E)

                else:
                    L, R = orpca(data_, rank=output_dimension, **kwargs)

                    loadings = L
                    factors = R.T

            elif algorithm == "ornmf":
                if return_info:
                    X, E, W, H = ornmf(
                        data_, rank=output_dimension, store_error=True, **kwargs,
                    )
                    to_return = (X, E)
                else:
                    W, H = ornmf(data_, rank=output_dimension, **kwargs)

                loadings = W
                factors = H.T

            elif is_sklearn_like:
                if hasattr(estim, "fit_transform"):
                    loadings = estim.fit_transform(data_)
                elif hasattr(estim, "fit") and hasattr(estim, "transform"):
                    estim.fit(data_)
                    loadings = estim.transform(data_)

                # Handle sklearn.pipeline.Pipeline objects
                # by taking the last step
                if hasattr(estim, "steps"):
                    estim_ = estim[-1]
                # Handle GridSearchCV and related objects
                # by taking the best estimator
                elif hasattr(estim, "best_estimator_"):
                    estim_ = estim.best_estimator_
                # Handle the "usual case"
                else:
                    estim_ = estim

                # We need to the components_ to set factors
                if not hasattr(estim_, "components_"):
                    raise AttributeError(
                        f"Fitted estimator {str(estim_)} has no attribute 'components_'"
                    )

                factors = estim_.components_.T

                if hasattr(estim_, "explained_variance_"):
                    explained_variance = estim_.explained_variance_

                if hasattr(estim_, "mean_"):
                    mean = estim_.mean_
                    centre = "samples"

                # Return the full estimator object
                to_print.extend(["scikit-learn estimator:", estim])
                if return_info:
                    to_return = estim

            else:
                raise ValueError("'algorithm' not recognised")

            # We must calculate the ratio here because otherwise the sum
            # information can be lost if the user subsequently calls
            # crop_decomposition_dimension()
            if explained_variance is not None and explained_variance_ratio is None:
                explained_variance_ratio = explained_variance / explained_variance.sum()
                number_significant_components = (
                    self.estimate_elbow_position(explained_variance_ratio) + 1
                )

            # Store the results in learning_results
            target.factors = factors
            target.loadings = loadings
            target.explained_variance = explained_variance
            target.explained_variance_ratio = explained_variance_ratio
            target.number_significant_components = number_significant_components
            target.decomposition_algorithm = algorithm
            target.poissonian_noise_normalized = normalize_poissonian_noise
            target.output_dimension = output_dimension
            target.unfolded = self._unfolded4decomposition
            target.centre = centre
            target.mean = mean

            if output_dimension and factors.shape[1] != output_dimension:
                target.crop_decomposition_dimension(output_dimension)

            # Delete the unmixing information, as it will refer to a
            # previous decomposition
            target.unmixing_matrix = None
            target.bss_algorithm = None

            if self._unfolded4decomposition:
                folding = self.metadata._HyperSpy.Folding
                target.original_shape = folding.original_shape

            # Reproject
            if mean is None:
                mean = 0

            if reproject in ("navigation", "both"):
                if not is_sklearn_like:
                    loadings_ = (dc[:, signal_mask] - mean) @ factors
                else:
                    loadings_ = estim.transform(dc[:, signal_mask])
                target.loadings = loadings_

            if reproject in ("signal", "both"):
                if not is_sklearn_like:
                    factors = (
                        np.linalg.pinv(loadings) @ (dc[navigation_mask, :] - mean)
                    ).T
                    target.factors = factors
                else:
                    warnings.warn(
                        "Reprojecting the signal is not yet "
                        f"supported for algorithm='{algorithm}'",
                        UserWarning,
                    )
                    if reproject == "both":
                        reproject = "signal"
                    else:
                        reproject = None

            # Rescale the results if the noise was normalized
            if normalize_poissonian_noise:
                target.factors[:] *= self._root_bH.T
                target.loadings[:] *= self._root_aG

            # Set the pixels that were not processed to nan
            if not isinstance(signal_mask, slice):
                # Store the (inverted, as inputed) signal mask
                target.signal_mask = ~signal_mask.reshape(
                    self.axes_manager._signal_shape_in_array
                )
                if reproject not in ("both", "signal"):
                    factors = np.zeros((dc.shape[-1], target.factors.shape[1]))
                    factors[signal_mask, :] = target.factors
                    factors[~signal_mask, :] = np.nan
                    target.factors = factors

            if not isinstance(navigation_mask, slice):
                # Store the (inverted, as inputed) navigation mask
                target.navigation_mask = ~navigation_mask.reshape(
                    self.axes_manager._navigation_shape_in_array
                )
                if reproject not in ("both", "navigation"):
                    loadings = np.zeros((dc.shape[0], target.loadings.shape[1]))
                    loadings[navigation_mask, :] = target.loadings
                    loadings[~navigation_mask, :] = np.nan
                    target.loadings = loadings

        finally:
            if self._unfolded4decomposition:
                self.fold()
                self._unfolded4decomposition = False
            self.learning_results.__dict__.update(target.__dict__)

            # Undo any pre-treatments by restoring the copied data
            if copy:
                self.undo_treatments()

        # Print details about the decomposition we just performed
        if print_info:
            print("\n".join([str(pr) for pr in to_print]))

        return to_return

    def blind_source_separation(
        self,
        number_of_components=None,
        algorithm="sklearn_fastica",
        diff_order=1,
        diff_axes=None,
        factors=None,
        comp_list=None,
        mask=None,
        on_loadings=False,
        reverse_component_criterion="factors",
        whiten_method="pca",
        return_info=False,
        print_info=True,
        **kwargs,
    ):
        """Apply blind source separation (BSS) to the result of a decomposition.

        The results are stored in ``self.learning_results``.

        Read more in the :ref:`User Guide <mva.blind_source_separation>`.

        Parameters
        ----------
        number_of_components : int or None
            Number of principal components to pass to the BSS algorithm.
            If None, you must specify the ``comp_list`` argument.
        algorithm : {"sklearn_fastica", "orthomax", "FastICA", "JADE", "CuBICA", "TDSEP", custom object}, default "sklearn_fastica"
            The BSS algorithm to use. If algorithm is an object,
            it must implement a ``fit_transform()`` method or ``fit()`` and
            ``transform()`` methods, in the same manner as a scikit-learn estimator.
        diff_order : int, default 1
            Sometimes it is convenient to perform the BSS on the derivative of
            the signal. If ``diff_order`` is 0, the signal is not differentiated.
        diff_axes : None or list of ints or strings
            * If None and `on_loadings` is False, when `diff_order` is greater than 1
              and `signal_dimension` is greater than 1, the differences are calculated
              across all signal axes
            * If None and `on_loadings` is True, when `diff_order` is greater than 1
              and `navigation_dimension` is greater than 1, the differences are calculated
              across all navigation axes
            * Otherwise the axes can be specified in a list.
        factors : :py:class:`~hyperspy.signal.BaseSignal` or numpy array
            Factors to decompose. If None, the BSS is performed on the
            factors of a previous decomposition. If a Signal instance, the
            navigation dimension must be 1 and the size greater than 1.
        comp_list : None or list or numpy array
            Choose the components to apply BSS to. Unlike ``number_of_components``,
            this argument permits non-contiguous components.
        mask : :py:class:`~hyperspy.signal.BaseSignal` or subclass
            If not None, the signal locations marked as True are masked. The
            mask shape must be equal to the signal shape
            (navigation shape) when `on_loadings` is False (True).
        on_loadings : bool, default False
            If True, perform the BSS on the loadings of a previous
            decomposition, otherwise, perform the BSS on the factors.
        reverse_component_criterion : {"factors", "loadings"}, default "factors"
            Use either the factors or the loadings to determine if the
            component needs to be reversed.
        whiten_method : {"pca", "zca", None}, default "pca"
            How to whiten the data prior to blind source separation.
            If None, no whitening is applied. See :py:func:`~.learn.whitening.whiten_data`
            for more details.
        return_info: bool, default False
            The result of the decomposition is stored internally. However,
            some algorithms generate some extra information that is not
            stored. If True, return any extra information if available.
            In the case of sklearn.decomposition objects, this includes the
            sklearn Estimator object.
        print_info : bool, default True
            If True, print information about the decomposition being performed.
            In the case of sklearn.decomposition objects, this includes the
            values of all arguments of the chosen sklearn algorithm.
        **kwargs : extra keyword arguments
            Any keyword arguments are passed to the BSS algorithm.

        Returns
        -------
        return_info : sklearn.Estimator or None
            * If True and 'algorithm' is an sklearn Estimator, returns the
              Estimator object.
            * Otherwise, returns None

        See Also
        --------
        * :py:meth:`~.signal.MVATools.plot_bss_factors`
        * :py:meth:`~.signal.MVATools.plot_bss_loadings`
        * :py:meth:`~.signal.MVATools.plot_bss_results`

        """
        from hyperspy.signal import BaseSignal

        lr = self.learning_results

        if factors is None:
            if not hasattr(lr, "factors") or lr.factors is None:
                raise AttributeError(
                    "A decomposition must be performed before blind "
                    "source separation, or factors must be provided."
                )
            else:
                if on_loadings:
                    factors = self.get_decomposition_loadings()
                else:
                    factors = self.get_decomposition_factors()

        if hasattr(factors, "compute"):
            # if the factors are lazy, we compute them, which should be fine
            # since we already reduce the dimensionality of the data.
            factors.compute()

        # Check factors
        if not isinstance(factors, BaseSignal):
            raise TypeError(
                "`factors` must be a BaseSignal instance, but an object "
                f"of type {type(factors)} was provided"
            )

        # Check factor dimensions
        if factors.axes_manager.navigation_dimension != 1:
            raise ValueError(
                "`factors` must have navigation dimension == 1, "
                "but the navigation dimension of the given factors "
                f"is {factors.axes_manager.navigation_dimension}"
            )
        elif factors.axes_manager.navigation_size < 2:
            raise ValueError(
                "`factors` must have navigation size"
                "greater than one, but the navigation "
                "size of the given factors "
                f"is {factors.axes_manager.navigation_size}"
            )

        # Check mask dimensions
        if mask is not None:
            ref_shape, space = (
                factors.axes_manager.signal_shape,
                "navigation" if on_loadings else "signal",
            )
            if isinstance(mask, BaseSignal):
                if mask.axes_manager.signal_shape != ref_shape:
                    raise ValueError(
                        f"`mask` shape is not equal to {space} shape. "
                        f"Mask shape: {mask.axes_manager.signal_shape}\t"
                        f"{space} shape: {ref_shape}"
                    )
            if hasattr(mask, "compute"):
                # if the mask is lazy, we compute them, which should be fine
                # since we already reduce the dimensionality of the data.
                mask.compute()

        # Note that we don't check the factor's signal dimension. This is on
        # purpose as an user may like to apply pretreaments that change their
        # dimensionality.

        # The diff_axes are given for the main signal. We need to compute
        # the correct diff_axes for the factors.
        # Get diff_axes index in axes manager
        if diff_axes is not None:
            diff_axes = [
                1 + axis.index_in_axes_manager
                for axis in [self.axes_manager[axis] for axis in diff_axes]
            ]
            if not on_loadings:
                diff_axes = [
                    index - self.axes_manager.navigation_dimension
                    for index in diff_axes
                ]

        # Select components to separate
        if number_of_components is not None:
            comp_list = range(number_of_components)
        elif comp_list is not None:
            number_of_components = len(comp_list)
        else:
            if lr.output_dimension is not None:
                number_of_components = lr.output_dimension
                comp_list = range(number_of_components)
            else:
                raise ValueError("No `number_of_components` or `comp_list` provided")

        factors = stack([factors.inav[i] for i in comp_list])

        # Check sklearn-like algorithms
        is_sklearn_like = False
        algorithms_sklearn = ["sklearn_fastica"]
        if algorithm in algorithms_sklearn:
            if not import_sklearn.sklearn_installed:
                raise ImportError(f"algorithm='{algorithm}' requires scikit-learn")

            # Set smaller convergence tolerance than sklearn default
            if not kwargs.get("tol", False):
                kwargs["tol"] = 1e-10

            # Initialize the sklearn estimator
            is_sklearn_like = True
            estim = decomposition_algorithms[algorithm](**kwargs)

            # Check whiten argument
            if estim.whiten and whiten_method is not None:
                _logger.warning(
                    "HyperSpy already performs its own data whitening "
                    f"(whiten_method='{whiten_method}'), so it is ignored "
                    f"for algorithm='{algorithm}'"
                )
                estim.whiten = False

        elif hasattr(algorithm, "fit_transform") or (
            hasattr(algorithm, "fit") and hasattr(algorithm, "transform")
        ):
            # Check properties of algorithm against typical sklearn objects
            # If algorithm is an object that implements the methods fit(),
            # transform() and fit_transform(), then we can use it like an
            # sklearn estimator. This also allows us to, for example, use
            # Pipeline and GridSearchCV objects.
            is_sklearn_like = True
            estim = algorithm

        # Initialize return_info and print_info
        to_return = None
        to_print = [
            "Blind source separation info:",
            f"  number_of_components={number_of_components}",
            f"  algorithm={algorithm}",
            f"  diff_order={diff_order}",
            f"  reverse_component_criterion={reverse_component_criterion}",
            f"  whiten_method={whiten_method}",
        ]

        # Apply differences pre-processing if requested.
        if diff_order > 0:
            factors = _get_derivative(
                factors, diff_axes=diff_axes, diff_order=diff_order
            )
            if mask is not None:
                # The following is a little trick to dilate the mask as
                # required when operation on the differences. It exploits the
                # fact that np.diff autimatically "dilates" nans. The trick has
                # a memory penalty which should be low compare to the total
                # memory required for the core application in most cases.
                mask_diff_axes = (
                    [iaxis - 1 for iaxis in diff_axes]
                    if diff_axes is not None
                    else None
                )
                mask.change_dtype("float")
                mask.data[mask.data == 1] = np.nan
                mask = _get_derivative(
                    mask, diff_axes=mask_diff_axes, diff_order=diff_order
                )
                mask.data[np.isnan(mask.data)] = 1
                mask.change_dtype("bool")

        # Unfold in case the signal_dimension > 1
        factors.unfold()
        if mask is not None:
            mask.unfold()
            factors = factors.data.T[np.where(~mask.data)]
        else:
            factors = factors.data.T

        # Center and whiten the data via PCA or ZCA methods
        if whiten_method is not None:
            _logger.info(f"Whitening the data with method '{whiten_method}'")

            factors, invsqcovmat = whiten_data(
                factors, centre=True, method=whiten_method
            )

        # Perform BSS
        if algorithm == "orthomax":
            _, unmixing_matrix = orthomax(factors, **kwargs)
            lr.bss_node = None

        elif algorithm in ["FastICA", "JADE", "CuBICA", "TDSEP"]:
            if not mdp_installed:
                raise ImportError(f"algorithm='{algorithm}' requires MDP toolbox")

            temp_function = getattr(mdp.nodes, algorithm + "Node")
            lr.bss_node = temp_function(**kwargs)
            lr.bss_node.train(factors)
            unmixing_matrix = lr.bss_node.get_recmatrix()

            to_print.extend(["mdp estimator:", lr.bss_node])
            if return_info:
                to_return = lr.bss_node

        elif is_sklearn_like:
            if hasattr(estim, "fit_transform"):
                _ = estim.fit_transform(factors)
            elif hasattr(estim, "fit") and hasattr(estim, "transform"):
                estim.fit(factors)

            # Handle sklearn.pipeline.Pipeline objects
            # by taking the last step
            if hasattr(estim, "steps"):
                estim_ = estim[-1]
            # Handle GridSearchCV and related objects
            # by taking the best estimator
            elif hasattr(estim, "best_estimator_"):
                estim_ = estim.best_estimator_
            # Handle the "usual case"
            else:
                estim_ = estim

            # We need to the components_ to set factors
            if hasattr(estim_, "components_"):
                unmixing_matrix = estim_.components_
            elif hasattr(estim_, "unmixing_matrix_"):
                # unmixing_matrix_ was renamed to components_ for FastICA
                # https://github.com/scikit-learn/scikit-learn/pull/858,
                # so this legacy only
                unmixing_matrix = estim_.unmixing_matrix_
            else:
                raise AttributeError(
                    f"Fitted estimator {str(estim_)} has no attribute 'components_'"
                )

            to_print.extend(["scikit-learn estimator:", estim])
            if return_info:
                to_return = estim

            # Store the BSS node
            lr.bss_node = estim

        else:
            raise ValueError("'algorithm' not recognised")

        # Apply the whitening matrix to get the full unmixing matrix
        if whiten_method is not None:
            w = unmixing_matrix @ invsqcovmat
        else:
            w = unmixing_matrix

        if lr.explained_variance is not None:
            if hasattr(lr.explained_variance, "compute"):
                lr.explained_variance = lr.explained_variance.compute()

            # The output of ICA is not sorted in any way what makes it
            # difficult to compare results from different unmixings. The
            # following code is an experimental attempt to sort them in a
            # more predictable way
            sorting_indices = np.argsort(
                lr.explained_variance[:number_of_components] @ np.abs(w.T)
            )[::-1]
            w[:] = w[sorting_indices, :]

        lr.unmixing_matrix = w
        lr.on_loadings = on_loadings
        self._unmix_components()
        self._auto_reverse_bss_component(reverse_component_criterion)
        lr.bss_algorithm = algorithm
        lr.bss_node = str(lr.bss_node)

        # Print details about the BSS we just performed
        if print_info:
            print("\n".join([str(pr) for pr in to_print]))

        return to_return

    def normalize_decomposition_components(self, target="factors", function=np.sum):
        """Normalize decomposition components.

        Parameters
        ----------
        target : {"factors", "loadings"}
            Normalize components based on the scale of either the factors or loadings.
        function : numpy universal function, default np.sum
            Each target component is divided by the output of ``function(target)``.
            The function must return a scalar when operating on numpy arrays and
            must have an `axis` argument.

        """
        if target == "factors":
            target = self.learning_results.factors
            other = self.learning_results.loadings
        elif target == "loadings":
            target = self.learning_results.loadings
            other = self.learning_results.factors
        else:
            raise ValueError('target must be "factors" or "loadings"')

        if target is None:
            raise ValueError("This method can only be called after s.decomposition()")

        _normalize_components(target=target, other=other, function=function)

    def normalize_bss_components(self, target="factors", function=np.sum):
        """Normalize BSS components.

        Parameters
        ----------
        target : {"factors", "loadings"}
            Normalize components based on the scale of either the factors or loadings.
        function : numpy universal function, default np.sum
            Each target component is divided by the output of ``function(target)``.
            The function must return a scalar when operating on numpy arrays and
            must have an `axis` argument.

        """
        if target == "factors":
            target = self.learning_results.bss_factors
            other = self.learning_results.bss_loadings
        elif target == "loadings":
            target = self.learning_results.bss_loadings
            other = self.learning_results.bss_factors
        else:
            raise ValueError('target must be "factors" or "loadings"')

        if target is None:
            raise ValueError(
                "This method can only be called after s.blind_source_separation()"
            )

        _normalize_components(target=target, other=other, function=function)

    def reverse_decomposition_component(self, component_number):
        """Reverse the decomposition component.

        Parameters
        ----------
        component_number : list or int
            component index/es

        Examples
        --------
        >>> s = hs.load('some_file')
        >>> s.decomposition(True) # perform PCA
        >>> s.reverse_decomposition_component(1) # reverse IC 1
        >>> s.reverse_decomposition_component((0, 2)) # reverse ICs 0 and 2

        """
        if hasattr(self.learning_results.factors, "compute"):
            _logger.warning(
                f"Component(s) {component_number} not reversed, "
                "feature not implemented for lazy computations"
            )
        else:
            target = self.learning_results

            for i in [component_number]:
                _logger.info(f"Component {i} reversed")
                target.factors[:, i] *= -1
                target.loadings[:, i] *= -1

    def reverse_bss_component(self, component_number):
        """Reverse the independent component.

        Parameters
        ----------
        component_number : list or int
            component index/es

        Examples
        --------
        >>> s = hs.load('some_file')
        >>> s.decomposition(True) # perform PCA
        >>> s.blind_source_separation(3)  # perform ICA on 3 PCs
        >>> s.reverse_bss_component(1) # reverse IC 1
        >>> s.reverse_bss_component((0, 2)) # reverse ICs 0 and 2

        """
        if hasattr(self.learning_results.bss_factors, "compute"):
            _logger.warning(
                f"Component(s) {component_number} not reversed, "
                "feature not implemented for lazy computations"
            )
        else:
            target = self.learning_results

            for i in [component_number]:
                _logger.info(f"Component {i} reversed")
                target.bss_factors[:, i] *= -1
                target.bss_loadings[:, i] *= -1
                target.unmixing_matrix[i, :] *= -1

    def _unmix_components(self, compute=False):
        lr = self.learning_results
        w = lr.unmixing_matrix
        n = len(w)

        try:
            w_inv = np.linalg.inv(w)
        except np.linalg.LinAlgError as e:
            if "Singular matrix" in str(e):
                warnings.warn(
                    "Cannot invert unmixing matrix as it is singular. "
                    "Will attempt to use np.linalg.pinv instead.",
                    UserWarning,
                )
                w_inv = np.linalg.pinv(w)
            else:
                raise

        if lr.on_loadings:
            lr.bss_loadings = lr.loadings[:, :n] @ w.T
            lr.bss_factors = lr.factors[:, :n] @ w_inv
        else:
            lr.bss_factors = lr.factors[:, :n] @ w.T
            lr.bss_loadings = lr.loadings[:, :n] @ w_inv
        if compute:
            lr.bss_factors = lr.bss_factors.compute()
            lr.bss_loadings = lr.bss_loadings.compute()

    def _auto_reverse_bss_component(self, reverse_component_criterion):
        n_components = self.learning_results.bss_factors.shape[1]
        for i in range(n_components):
            if reverse_component_criterion == "factors":
                values = self.learning_results.bss_factors
            elif reverse_component_criterion == "loadings":
                values = self.learning_results.bss_loadings
            else:
                raise ValueError(
                    "`reverse_component_criterion` can take only "
                    "`factor` or `loading` as parameter."
                )
            minimum = np.nanmin(values[:, i])
            maximum = np.nanmax(values[:, i])
            if minimum < 0 and -minimum > maximum:
                self.reverse_bss_component(i)
                _logger.info(
                    f"Independent component {i} reversed based "
                    f"on the {reverse_component_criterion}"
                )

    def _calculate_recmatrix(self, components=None, mva_type="decomposition"):
        """Rebuilds data from selected components.

        Parameters
        ----------
        components : None, int, or list of ints
            * If None, rebuilds signal instance from all components
            * If int, rebuilds signal instance from components in range 0-given int
            * If list of ints, rebuilds signal instance from only components in given list
        mva_type : str {'decomposition', 'bss'}
            Decomposition type (not case sensitive)

        Returns
        -------
        Signal instance
            Data built from the given components.

        """

        target = self.learning_results

        if mva_type.lower() == "decomposition":
            factors = target.factors
            loadings = target.loadings.T
        elif mva_type.lower() == "bss":
            factors = target.bss_factors
            loadings = target.bss_loadings.T

        if components is None:
            a = factors @ loadings
            signal_name = f"model from {mva_type} with {factors.shape[1]} components"
        elif hasattr(components, "__iter__"):
            tfactors = np.zeros((factors.shape[0], len(components)))
            tloadings = np.zeros((len(components), loadings.shape[1]))
            for i in range(len(components)):
                tfactors[:, i] = factors[:, components[i]]
                tloadings[i, :] = loadings[components[i], :]
            a = tfactors @ tloadings
            signal_name = f"model from {mva_type} with components {components}"
        else:
            a = factors[:, :components] @ loadings[:components, :]
            signal_name = f"model from {mva_type} with {components} components"

        self._unfolded4decomposition = self.unfold()
        try:
            sc = self.deepcopy()
            sc.data = a.T.reshape(self.data.shape)
            sc.metadata.General.title += " " + signal_name
            if target.mean is not None:
                sc.data += target.mean
        finally:
            if self._unfolded4decomposition:
                self.fold()
                sc.fold()
                self._unfolded4decomposition = False

        return sc

    def get_decomposition_model(self, components=None):
        """Generate model with the selected number of principal components.

        Parameters
        ----------
        components : {None, int, list of ints}, default None
            * If None, rebuilds signal instance from all components
            * If int, rebuilds signal instance from components in range 0-given int
            * If list of ints, rebuilds signal instance from only components in given list

        Returns
        -------
        Signal instance
            A model built from the given components.

        """
        rec = self._calculate_recmatrix(components=components, mva_type="decomposition")
        return rec

    def get_bss_model(self, components=None, chunks="auto"):
        """Generate model with the selected number of independent components.

        Parameters
        ----------
        components : {None, int, list of ints}, default None
            * If None, rebuilds signal instance from all components
            * If int, rebuilds signal instance from components in range 0-given int
            * If list of ints, rebuilds signal instance from only components in given list

        Returns
        -------
        Signal instance
            A model built from the given components.

        """
        lr = self.learning_results
        if self._lazy:
            if isinstance(lr.bss_factors, np.ndarray):
                lr.factors = da.from_array(lr.bss_factors, chunks=chunks)
            if isinstance(lr.bss_factors, np.ndarray):
                lr.loadings = da.from_array(lr.bss_loadings, chunks=chunks)
        rec = self._calculate_recmatrix(components=components, mva_type="bss")
        return rec

    def get_explained_variance_ratio(self):
        """Return explained variance ratio of the PCA components as a Signal1D.

        Read more in the :ref:`User Guide <mva.scree_plot>`.

        Returns
        -------
        s : Signal1D
            Explained variance ratio.

        See Also
        --------
        * :py:meth:`~.learn.mva.MVA.decomposition`
        * :py:meth:`~.learn.mva.MVA.plot_explained_variance_ratio`
        * :py:meth:`~.learn.mva.MVA.get_decomposition_loadings`
        * :py:meth:`~.learn.mva.MVA.get_decomposition_factors`

        """
        from hyperspy._signals.signal1d import Signal1D

        target = self.learning_results
        if target.explained_variance_ratio is None:
            raise AttributeError(
                "The explained_variance_ratio attribute is "
                "`None`, did you forget to perform a PCA "
                "decomposition?"
            )
        s = Signal1D(target.explained_variance_ratio)
        s.metadata.General.title = self.metadata.General.title + "\nPCA Scree Plot"
        s.axes_manager[-1].name = "Principal component index"
        s.axes_manager[-1].units = ""
        return s

    def plot_explained_variance_ratio(
        self,
        n=30,
        log=True,
        threshold=0,
        hline="auto",
        vline=False,
        xaxis_type="index",
        xaxis_labeling=None,
        signal_fmt=None,
        noise_fmt=None,
        fig=None,
        ax=None,
        **kwargs,
    ):
        """Plot the decomposition explained variance ratio vs index number.

        This is commonly known as a scree plot.

        Read more in the :ref:`User Guide <mva.scree_plot>`.

        Parameters
        ----------
        n : int or None
            Number of components to plot. If None, all components will be plot
        log : bool, default True
            If True, the y axis uses a log scale.
        threshold : float or int
            Threshold used to determine how many components should be
            highlighted as signal (as opposed to noise).
            If a float (between 0 and 1), ``threshold`` will be
            interpreted as a cutoff value, defining the variance at which to
            draw a line showing the cutoff between signal and noise;
            the number of signal components will be automatically determined
            by the cutoff value.
            If an int, ``threshold`` is interpreted as the number of
            components to highlight as signal (and no cutoff line will be
            drawn)
        hline: {'auto', True, False}
            Whether or not to draw a horizontal line illustrating the variance
            cutoff for signal/noise determination. Default is to draw the line
            at the value given in ``threshold`` (if it is a float) and not
            draw in the case  ``threshold`` is an int, or not given.
            If True, (and ``threshold`` is an int), the line will be drawn
            through the last component defined as signal.
            If False, the line will not be drawn in any circumstance.
        vline: bool, default False
            Whether or not to draw a vertical line illustrating an estimate of
            the number of significant components. If True, the line will be
            drawn at the the knee or elbow position of the curve indicating the
            number of significant components.
            If False, the line will not be drawn in any circumstance.
        xaxis_type : {'index', 'number'}
            Determines the type of labeling applied to the x-axis.
            If ``'index'``, axis will be labeled starting at 0 (i.e.
            "pythonic index" labeling); if ``'number'``, it will start at 1
            (number labeling).
        xaxis_labeling : {'ordinal', 'cardinal', None}
            Determines the format of the x-axis tick labels. If ``'ordinal'``,
            "1st, 2nd, ..." will be used; if ``'cardinal'``, "1, 2,
            ..." will be used. If None, an appropriate default will be
            selected.
        signal_fmt : dict
            Dictionary of matplotlib formatting values for the signal
            components
        noise_fmt : dict
            Dictionary of matplotlib formatting values for the noise
            components
        fig : matplotlib figure or None
            If None, a default figure will be created, otherwise will plot
            into fig
        ax : matplotlib ax (subplot) or None
            If None, a default ax will be created, otherwise will plot into ax
        **kwargs
            remaining keyword arguments are passed to ``matplotlib.figure()``

        Returns
        -------
        ax : matplotlib.axes
            Axes object containing the scree plot

        Example
        -------
        To generate a scree plot with customized symbols for signal vs.
        noise components and a modified cutoff threshold value:

        >>> s = hs.load("some_spectrum_image")
        >>> s.decomposition()
        >>> s.plot_explained_variance_ratio(n=40,
        >>>                                 threshold=0.005,
        >>>                                 signal_fmt={'marker': 'v',
        >>>                                             's': 150,
        >>>                                             'c': 'pink'}
        >>>                                 noise_fmt={'marker': '*',
        >>>                                             's': 200,
        >>>                                             'c': 'green'})

        See Also
        --------
        * :py:meth:`~.learn.mva.MVA.decomposition`
        * :py:meth:`~.learn.mva.MVA.get_explained_variance_ratio`
        * :py:meth:`~.signal.MVATools.get_decomposition_loadings`
        * :py:meth:`~.signal.MVATools.get_decomposition_factors`

        """
        s = self.get_explained_variance_ratio()

        n_max = len(self.learning_results.explained_variance_ratio)
        if n is None:
            n = n_max
        elif n > n_max:
            _logger.info("n is too large, setting n to its maximal value.")
            n = n_max

        # Determine right number of components for signal and cutoff value
        if isinstance(threshold, float):
            if not 0 < threshold < 1:
                raise ValueError("Variance threshold should be between 0 and" " 1")
            # Catch if the threshold is less than the minimum variance value:
            if threshold < s.data.min():
                n_signal_pcs = n
            else:
                n_signal_pcs = np.where((s < threshold).data)[0][0]
        else:
            n_signal_pcs = threshold
            if n_signal_pcs == 0:
                hline = False

        if vline:
            if self.learning_results.number_significant_components is None:
                vline = False
            else:
                index_number_significant_components = (
                    self.learning_results.number_significant_components - 1
                )
        else:
            vline = False

        # Handling hline logic
        if hline == "auto":
            # Set cutoff to threshold if float
            if isinstance(threshold, float):
                cutoff = threshold
            # Turn off the hline otherwise
            else:
                hline = False
        # If hline is True and threshold is int, set cutoff at value of last
        # signal component
        elif hline:
            if isinstance(threshold, float):
                cutoff = threshold
            elif n_signal_pcs > 0:
                cutoff = s.data[n_signal_pcs - 1]
        # Catches hline==False and hline==True (if threshold not given)
        else:
            hline = False

        # Some default formatting for signal markers
        if signal_fmt is None:
            signal_fmt = {
                "c": "#C24D52",
                "linestyle": "",
                "marker": "^",
                "markersize": 10,
                "zorder": 3,
            }

        # Some default formatting for noise markers
        if noise_fmt is None:
            noise_fmt = {
                "c": "#4A70B0",
                "linestyle": "",
                "marker": "o",
                "markersize": 10,
                "zorder": 3,
            }

        # Sane defaults for xaxis labeling
        if xaxis_labeling is None:
            xaxis_labeling = "cardinal" if xaxis_type == "index" else "ordinal"

        axes_titles = {
            "y": "Proportion of variance",
            "x": f"Principal component {xaxis_type}",
        }

        if n < s.axes_manager[-1].size:
            s = s.isig[:n]

        if fig is None:
            fig = plt.figure(**kwargs)

        if ax is None:
            ax = fig.add_subplot(111)

        if log:
            ax.set_yscale("log")

        if hline:
            ax.axhline(cutoff, linewidth=2, color="gray", linestyle="dashed", zorder=1)

        if vline:
            ax.axvline(
                index_number_significant_components,
                linewidth=2,
                color="gray",
                linestyle="dashed",
                zorder=1,
            )

        index_offset = 0
        if xaxis_type == "number":
            index_offset = 1

        if n_signal_pcs == n:
            ax.plot(
                range(index_offset, index_offset + n), s.isig[:n].data, **signal_fmt
            )
        elif n_signal_pcs > 0:
            ax.plot(
                range(index_offset, index_offset + n_signal_pcs),
                s.isig[:n_signal_pcs].data,
                **signal_fmt,
            )
            ax.plot(
                range(index_offset + n_signal_pcs, index_offset + n),
                s.isig[n_signal_pcs:n].data,
                **noise_fmt,
            )
        else:
            ax.plot(range(index_offset, index_offset + n), s.isig[:n].data, **noise_fmt)

        if xaxis_labeling == "cardinal":
            ax.xaxis.set_major_formatter(FuncFormatter(lambda x, p: ordinal(x)))

        ax.set_ylabel(axes_titles["y"])
        ax.set_xlabel(axes_titles["x"])
        ax.xaxis.set_major_locator(MaxNLocator(integer=True, min_n_ticks=1))
        ax.margins(0.05)
        ax.autoscale()
        ax.set_title(s.metadata.General.title, y=1.01)

        return ax

    def plot_cumulative_explained_variance_ratio(self, n=50):
        """Plot cumulative explained variance up to n principal components.

        Parameters
        ----------
        n : int
            Number of principal components to show.

        Returns
        -------
        ax : matplotlib.axes
            Axes object containing the cumulative explained variance plot.

        See Also
        --------
        :py:meth:`~.learn.mva.MVA.plot_explained_variance_ratio`,

        """
        target = self.learning_results
        if n > target.explained_variance.shape[0]:
            n = target.explained_variance.shape[0]
        cumu = np.cumsum(target.explained_variance) / np.sum(target.explained_variance)
        fig = plt.figure()
        ax = fig.add_subplot(111)
        ax.scatter(range(n), cumu[:n])
        ax.set_xlabel("Principal component")
        ax.set_ylabel("Cumulative explained variance ratio")
        plt.draw()

        return ax

    def normalize_poissonian_noise(self, navigation_mask=None, signal_mask=None):
        """Normalize the signal under the assumption of Poisson noise.

        Scales the signal using to "normalize" the Poisson data for
        subsequent decomposition analysis [Keenan2004]_.

        Parameters
        ----------
        navigation_mask : {None, boolean numpy array}, default None
            Optional mask applied in the navigation axis.
        signal_mask : {None, boolean numpy array}, default None
            Optional mask applied in the signal axis.

        """
        _logger.info("Scaling the data to normalize Poissonian noise")
        with self.unfolded():
            # The rest of the code assumes that the first data axis
            # is the navigation axis. We transpose the data if that
            # is not the case.
            if self.axes_manager[0].index_in_array == 0:
                dc = self.data
            else:
                dc = self.data.T

            if navigation_mask is None:
                navigation_mask = slice(None)
            else:
                navigation_mask = ~navigation_mask.ravel()
            if signal_mask is None:
                signal_mask = slice(None)
            else:
                signal_mask = ~signal_mask

            # Check non-negative
            if dc[:, signal_mask][navigation_mask, :].min() < 0.0:
                raise ValueError(
                    "Negative values found in data!\n"
                    "Are you sure that the data follow a Poisson distribution?"
                )

            # Rescale the data to normalize the Poisson noise
            aG = dc[:, signal_mask][navigation_mask, :].sum(1).squeeze()
            bH = dc[:, signal_mask][navigation_mask, :].sum(0).squeeze()

            self._root_aG = np.sqrt(aG)[:, np.newaxis]
            self._root_bH = np.sqrt(bH)[np.newaxis, :]

            # We ignore numpy's warning when the result of an
            # operation produces nans - instead we set 0/0 = 0
            with np.errstate(divide="ignore", invalid="ignore"):
                dc[:, signal_mask][navigation_mask, :] /= self._root_aG * self._root_bH
                dc[:, signal_mask][navigation_mask, :] = np.nan_to_num(
                    dc[:, signal_mask][navigation_mask, :]
                )

    def undo_treatments(self):
        """Undo Poisson noise normalization and other pre-treatments.

        Only valid if calling ``s.decomposition(..., copy=True)``.
        """
        if hasattr(self, "_data_before_treatments"):
            _logger.info("Undoing data pre-treatments")
            self.data[:] = self._data_before_treatments
            del self._data_before_treatments
        else:
            raise AttributeError(
                "Unable to undo data pre-treatments! Be sure to"
                "set `copy=True` when calling s.decomposition()."
            )


    def _mask_for_clustering(self,mask):
        # Deal with masks
        if hasattr(mask, 'ravel'):
            mask = mask.ravel()
    
        # Transform the None masks in slices to get the right behaviour
        if mask is None:
            mask = slice(None)
        else:
            mask = ~mask
    
        return mask

    def scale_data_for_clustering(self,
                                  use_decomposition_results=True,
                                  scaling="minmax",
                                  scaling_kwargs={},
                                  number_pca_components=None,
                                  navigation_mask=None,
                                  signal_mask=None):
        """scale data for cluster analysis

        Results are stored in `learning_results`.

        Parameters
        ----------
        n_clusters : int
            Number of clusters to find.
        use_decomposition_results : bool or numpy array
            If True (recommended) the signal's decomposition results are used
            for clustering. Note - If this option is not used the raw data
            is used. This can be memory intensive and is only recommened if
            the Signal has a small `signal_dimension`.
        scaling : {"standard","norm","minmax",None or scikit learn scaling method}
            default: 'minmax'
            Preprocessing the data before cluster analysis requires scaling
            the data to be clustered to similar scales. Standard scaling
            adjusts each feature to have uniform variation. Norm scaling
            adjusts treats the set of features like a vector and
            each measurement is scaled to length 1.
            You can also pass one of the scikit-learn preprocessing
            scale_method = import sklearn.processing.StandadScaler
            scaling = scale_method
            See scaling methods in scikit-learn preprocessing for further
            details.
        scaling_kwargs :
            Additional parameters passed to the cluster scaling algorithm.
            See sklearn.preprocessing scaling methods for further details
        number_pca_components: int , default None
            If you are clustering using the decomposition results
            (use_decomposition_results = True) you can define how many PCA
            components to use.   if set to None the method uses the
            estimate of significant components found in the decomposition step
            using the elbow method
            (learningresults.number_significant_components)
        navigation_mask : boolean numpy array
            The navigation locations marked as True are not used in the
            clustering.
        signal_mask : boolean numpy array
            The signal locations marked as True are not used in the
            clustering.  Note that if use_decomposition_results=True the
            signal_mask is ignored. The number of PCA components is used to
            set the number of components to use.

        Returns
        ----------
        scaled_data : numpy array - unfoled array of shape (number_of_samples,
        no_of_features) scaled according to the selected algorithm

        """

        if import_sklearn.sklearn_installed is False:
            raise ImportError(
                'sklearn is not installed. Nothing done')
        if scaling == "norm":
            algorithm = import_sklearn.sklearn.preprocessing.Normalizer
        elif scaling == "standard":
            algorithm = import_sklearn.sklearn.preprocessing.StandardScaler
        elif scaling == "minmax":
            algorithm = import_sklearn.sklearn.preprocessing.MinMaxScaler
        else:
            algorithm = scaling
            if scaling is not None:
                if inspect.isclass(scaling) \
                    and not issubclass(algorithm,
                                import_sklearn.sklearn.base.TransformerMixin):
                        raise ValueError("The class provided to the scaling"
                            "parameter must be a scikit-learn"
                            "preprocessing class.")

        if self.axes_manager.navigation_size < 2:
            raise AttributeError("It is not possible to cluster a dataset "
                                 "with navigation_size < 2")

        self._unfolded4clustering = self.unfold()
        # Deal with masks
        navigation_mask = self._mask_for_clustering(navigation_mask)
        if use_decomposition_results:
            dc = self.learning_results.loadings.copy()
            data = dc[:,slice(0,number_pca_components,1)]
            signal_mask =  self._mask_for_clustering(None)
        else:
            data = self.data
            signal_mask = self._mask_for_clustering(signal_mask)

        dc = data if self.axes_manager[0].index_in_array == 0 else data.T
        if algorithm == None:
            return dc[:, signal_mask][navigation_mask, :]
        else:
            scaler = algorithm(**scaling_kwargs)
            return scaler.fit_transform(dc[:, signal_mask][navigation_mask, :])

    def _get_number_pca_components_for_clustering(self):
        if self.learning_results.number_significant_components is None:
            raise ValueError("Number of pca components not defined, "
                             "please run decomposition first.")
        else:
            number_pca_components = self.learning_results.number_significant_components
        return number_pca_components

    def _get_number_clusters_for_clustering(self):
        if self.learning_results.number_of_clusters is None:
            raise ValueError("Number of clusters not defined, "
                             "please run evaluate_number_of_clusters first.")
        else:
            number_of_clusters = self.learning_results.number_of_clusters
        return number_of_clusters

    def _cluster_analysis(self,
                          n_clusters,
                          scaled_data,
                          algorithm='kmeans',
                          **kwargs,
                          ):
        """
        Cluster analysis of a scaled data - internal

        Parameters
        ----------
        n_clusters : int
            Number of clusters to find.
        scaled_data : numpy array - (number_of_samples,number_of_features)
        algorithm: string "kmeans" or "agglomerative"
             See scikit-learn documentation. Default "kmeans"
        **kwargs
            Additional parameters passed to the clustering algorithm.
            This may include `n_init`, the number of times the algorithm is
            restarted to optimize results.

        """
        if import_sklearn.sklearn_installed is False:
            raise ImportError('sklearn is not installed. Nothing done.')
        if algorithm not in cluster_algorithms:
            raise ValueError("Cluster algorithm {algorithm} not supported or "
                             "not correctly defined.")

        alg = cluster_algorithms[algorithm](n_clusters=n_clusters, **kwargs)

        alg.fit(scaled_data)

        return alg

    def plot_cluster_metric(self):
        """Plot the cluster metrics calculated
           using evaluate_number_of_clusters method

        """
        target = self.learning_results

        if target.cluster_metric_data is not None:
            ydata = target.cluster_metric_data
        else:
            raise ValueError("Cluster metrics not evaluated "
                             "please run evaluate_number_of_clusters first.")
        if target.cluster_metric_index is not None:
            xdata = target.cluster_metric_index
        nclusters = target.number_of_clusters
        fig = plt.figure()
        ax = fig.add_subplot(111)
        ax.scatter(xdata, ydata)
        ax.set_xlabel('number of clusters')
        label =  str(target.cluster_metric) +"_metric"
        ax.set_ylabel(label)
        if isinstance(nclusters, list):
            for nc in nclusters:
                ax.axvline(nc,
                    linewidth=2,
                    color='gray',
                    linestyle='dashed')
        else:
            ax.axvline(nclusters,
                linewidth=2,
                color='gray',
                linestyle='dashed')

        plt.draw()
        return ax

    def _create_cluster_centers_from_labels(self, labels,
                                           use_decomposition_for_centers=False,
                                           number_pca_components=None,
                                           navigation_mask=None):
        """
        From a set of cluster labels generate the cluster centers from the
        raw data or PCA decomposition results

        Parameters
        ----------
        labels : int array of length n_samples where each value is a cluster
            label from 0 to n_clusters-1
        use_decomposition_for_centers : bool
            If True (recommended) the pca results are used for building the
            the cluster centers from the clustered label results.
            If False the original signal data is used.
        number_pca_components : int, default None
            If you are getting the cluster centers using the decomposition
            results (use_decomposition_for_centers=True) you can define how
            many PCA components to use. If set to None the method uses the
            estimate of significant components found in the decomposition step
            using the elbow method and stored in the
            ``learning_results.number_significant_components`` attribute.

        Returns
        -------
        cluster_labels  : array  - (n_clusters, n_samples)
        cluster_centers : array  - (n_clusters, signal_shape)
        """

        if use_decomposition_for_centers and number_pca_components is None:
            number_pca_components = self._get_number_pca_components_for_clustering()

        if navigation_mask is None:
            navigation_mask = self._mask_for_clustering(navigation_mask)

        if (not self._unfolded4clustering and
                self.axes_manager.navigation_dimension > 1):
            raise ValueError("Data (and labels) must be unfolded.")

        # now re-organize the labels to fit with hyperspy loadings/factors
        # and use the kmeans centers to extract real data cluster centers
        # create an array to store the centers
        n_clusters = int(np.amax(labels)) + 1


        # From the cluster labels we know which parts of the signal correspond
        # to different clusters.
        #
        # We can produce the representative 1D or 2D signals for each cluster
        # by averaging all points with a given label or averaging
        # PCA components*loadings with a given label
        if not use_decomposition_for_centers:
            clusterdata = self.data \
                if self.axes_manager[0].index_in_array == 0 else self.data.T
            cluster_centers = np.zeros((n_clusters, clusterdata.shape[-1]))
        else:
            cluster_centers = np.zeros((n_clusters,
                                       self.learning_results.factors.shape[0]))

        #
        # create the centers to match
        #
        clustersizes = np.zeros((n_clusters,), dtype=np.int)
        for i in range(n_clusters):
            clus_index = np.where(labels == i)
            clustersizes[i] = labels[np.where(labels == i)].shape[0]
            # if using the pca components
            if use_decomposition_for_centers:
                # pca clustered...
                a = self.learning_results.loadings[clus_index][:,
                                                       0:number_pca_components]
                b = self.learning_results.factors[:, 0:number_pca_components].T
                center = np.dot(a, b).sum(axis=0)
                cluster_centers[i, :] = cluster_centers[i, :] + center
                cluster_centers[i, :] = cluster_centers[i, :] 
            else:
                cluster_centers[i, :] = clusterdata[clus_index].sum(axis=0)

        # this sorts the labels based on clustersize for high to low
        # i.e. point with laster number of points first
        idx = np.argsort(clustersizes)[::-1]
        lut = np.zeros_like(idx)
        lut[idx] = np.arange(n_clusters)
        sorted_labels = lut[labels]
        shape = (n_clusters, self.data.shape[0])
        cluster_labels = np.full(shape, np.nan)
        sorted_cluster_centers = np.zeros_like(cluster_centers)            
        # now create the labels from these sorted labels
        for i in range(n_clusters):
            cluster_labels[i, navigation_mask] = \
                np.where(sorted_labels == i, 1, 0)
            sorted_cluster_centers[i,:] = cluster_centers[lut[i],:]\
                /clustersizes[lut[i]]                



        return sorted_labels,cluster_labels, sorted_cluster_centers


    def cluster_analysis(self,
                         n_clusters=None,
                         scaling="minmax",
                         scaling_kwargs={},
                         use_decomposition_results=True,
                         use_decomposition_for_centers=False,
                         number_pca_components=None,
                         navigation_mask=None,
                         signal_mask=None,
                         algorithm='kmeans',
                         return_info=False,
                         **kwargs):
        """
        Cluster analysis of a signal or decomposition results of a signal

        Results are stored in `learning_results`.

        Parameters
        ----------
        n_clusters : int
            Number of clusters to find.
        scaling : {"standard","norm","minmax",None or scikit learn scaling method}
            default: 'minmax'
            Preprocessing the data before cluster analysis requires scaling
            the data to be clustered to similar scales. Standard scaling
            adjusts each feature to have uniform variation. Norm scaling
            adjusts treats the set of features like a vector and
            each measurement is scaled to length 1.
            You can also pass one of the scikit-learn preprocessing
            scale_method = import sklearn.processing.StandadScaler
            scaling = scale_method
            See scaling methods in scikit-learn preprocessing for further
            details.
        scaling_kwargs : dict
            Additional parameters passed to the cluster scaling algorithm.
            See sklearn.preprocessing scaling methods for further details
        use_decomposition_results : bool
            If True (recommended) the signal's decomposition results are used
            for clustering. Note - If this option is not used the raw data
            is used. This can be memory intensive and is only recommened if
            the Signal has a small `signal_dimension`.
        use_decomposition_for_centers : bool
            If True (recommended) the pca results are used for building the
            the cluster centers from the clustered label results.
            If False the original signal data is used.
        number_pca_components : int, default None
            If you are getting the cluster centers using the decomposition
            results (use_decomposition_for_centers=True) you can define how
            many PCA components to use. If set to None the method uses the
            estimate of significant components found in the decomposition step
            using the elbow method and stored in the
            ``learning_results.number_significant_components`` attribute.
        navigation_mask : boolean numpy array
            The navigation locations marked as True are not used in the
            decomposition.
        signal_mask : boolean numpy array
            The signal locations marked as True are not used in the
            decomposition.
        algorithm : { "kmeans" | "agglomerative" }
            See scikit-lear documentation. Default "kmeans"
        return_info : bool, default False
            The result of the cluster analysis is stored internally. However,
            the cluster class used  contain a number of attributes.
            If True (the default is False)
            return the cluster object so the attributes can be accessed.
        **kwargs : dict  optional, default - empty
            Additional parameters passed to the clustering algorithm.
            For example, in case of the "kmeans" algorithm, `n_init` can be
            used to define the number of times the algorithm is restarted to
            optimize results.

        Returns:
            If 'return_info' is True returns the Scikit-learn cluster object
            used for clustering. Useful if you wish to
            examine inertia or other outputs.
        """

        to_return = None
        # backup the original data
        self._data_before_treatments = self.data.copy()

        if use_decomposition_results and number_pca_components is None:
            number_pca_components = self._get_number_pca_components_for_clustering()

        if n_clusters is None:
            n_clusters = self._get_number_clusters_for_clustering()

        if n_clusters < 2:
            raise ValueError("The number of clusters, n_clusters "
                             "must be specified and be >= 2.")

        target = LearningResults()

        try:
            cluster_labels = None
            cluster_centers = None

            # scale the data before clustering
            scaled_data = self.scale_data_for_clustering(
                use_decomposition_results=use_decomposition_results,
                scaling=scaling,
                number_pca_components=number_pca_components,
                navigation_mask=navigation_mask,
                signal_mask=signal_mask)
            alg = self._cluster_analysis(n_clusters,
                                         scaled_data,
                                         algorithm,
                                         **kwargs)
            if return_info:
                to_return = alg

            labels = alg.labels_

            if self._unfolded4clustering is True:
                folding = self.metadata._HyperSpy.Folding
                target.original_shape = folding.original_shape

            # now re-organize the labels to fit with hyperspy loadings/factors
            # and use the kmeans centers to extract real data cluster centers
            # create an array to store the centers
            sorted_membership,cluster_labels, cluster_centers = \
                self._create_cluster_centers_from_labels(
                    labels,
                    use_decomposition_for_centers=use_decomposition_for_centers,
                    number_pca_components=number_pca_components,
                    navigation_mask=self._mask_for_clustering(navigation_mask)
                )
        finally:
            target.cluster_membership = sorted_membership
            target.cluster_labels = cluster_labels
            target.cluster_centers = cluster_centers
            target.number_of_clusters = n_clusters
            target.cluster_algorithm = algorithm
            self.learning_results.__dict__.update(target.__dict__)

            if self._unfolded4clustering is True:
                self.fold()
                self._unfolded4clustering = False

            # undo any pre-treatments
            self.undo_treatments()

        return to_return


    def estimate_number_of_clusters(self,
                                    max_clusters=12,
                                    scaling="minmax",
                                    scaling_kwargs={},
                                    use_decomposition_results=True,
                                    use_decomposition_for_centers=False,
                                    number_pca_components=None,
                                    navigation_mask=None,
                                    signal_mask=None,
                                    algorithm='kmeans',
                                    metric="gap",
                                    n_ref=10,
                                    **kwargs):
        """
        Performs cluster analysis of a signal for cluster sizes ranging from
        n_clusters =2 to max_clusters ( default 12)
        Note that this can be a slow process for large datasets so please
        consider reducing max_clusters in this case.
        For each cluster it evaluates the silhouette score which is a metric of
        how well seperated the clusters are. Maximima or peaks in the scores
        indicate good choices for cluster sizes.


        Parameters
        ----------
        max_clusters : int, default 12
            Max number of clusters to use. The method will scan from 2 to
            max_clusters. 
        scaling : {"standard","norm","minmax" or scikit learn scaling method}
            default: 'minmax'
            Preprocessing the data before cluster analysis requires scaling
            the data to be clustered to similar scales. Standard scaling
            adjusts each feature to have uniform variation. Norm scaling
            adjusts treats the set of features like a vector and
            each measurement is scaled to length 1.
            You can also pass one of the scikit-learn preprocessing
            scale_method = import sklearn.processing.StandadScaler
            scaling = scale_method
            See scaling methods in scikit-learn preprocessing for further
            details.
        scaling_kwargs : dict, default empty
            Additional parameters passed to the cluster scaling algorithm.
            See sklearn.preprocessing scaling methods for further details
        use_decomposition_results : bool, default : True
            If True (recommended) the signal's decomposition results are used
            for clustering. Note - If this option is not used the raw data
            is used. This can be memory intensive and is only recommened if
            the Signal has a small `signal_dimension`.
        use_decomposition_for_centers : bool
            If True (recommended) the pca results are used for building the
            the cluster centers from the clustered label results.
            If False the original signal data is used.
        number_pca_components : int, default None
            If you are getting the cluster centers using the decomposition
            results (use_decomposition_for_centers=True) you can define how
            many PCA components to use. If set to None the method uses the
            estimate of significant components found in the decomposition step
            using the elbow method and stored in the
            ``learning_results.number_significant_components`` attribute.
        navigation_mask : boolean numpy array, default : None
            The navigation locations marked as True are not used in the
            decomposition.
        signal_mask : boolean numpy array, default : None
            The signal locations marked as True are not used in the
            decomposition.
        metric : {'elbow','silhouette','gap'} default 'gap'
            Use distance,silhouette analysis or gap statistics to estimate
            the optimal number of clusters.
            Gap is believed to be, overall, the best metric but it's also
            the slowest. Elbow measures the distances between points in
            each cluster as an estimate of how well grouped they are and
            is the fastest metric.
            For elbow the optimal k is the knee or elbow point.
            For gap the optimal k is the first k gap(k)>= gap(k+1)-std_error
            For silhouette the optimal k will be one of the "maxima" found with
            this method
        n_ref :  int, default 5
            Number of random references to use in gap statistics method
            Gap statistics compares the results from clustering the data to
            clustering random data. This random clustering is
            typically averaged n_ref times to get an statistical average
        **kwargs : dict {}  default empty
            Additional parameters passed to the clustering algorithm.

        See also
        --------
        :py:meth:`~.learn.mva.MVA.plot_cluster_metric`,

        """
        def distances_within_cluster(data,memberships,squared=True):
            return [np.sum(
                import_sklearn.sklearn.metrics.pairwise.euclidean_distances(
                data[memberships == c, :]
                -np.mean(data[memberships == c],axis=0),
                squared=squared)/
                (2.*data[memberships == c, :].shape[0]))
                for c in np.unique(memberships)]

        if use_decomposition_results and number_pca_components is None:
            number_pca_components = self._get_number_pca_components_for_clustering()

        if max_clusters < 2:
            raise ValueError("The number of clusters, max_clusters "
                             "must be specified and be >= 2.")

        to_return = None
        best_k    = None
        k_range   = list(range(1, max_clusters+1))
        #
        # for silhouette k starts at 2
        # for kmeans or gap k starts at 1
        # As these methods use random numbers we need to
        # initiate the random number generator to ensure
        # consistent/repeatable results
        #
        if(algorithm == "agglomerative"):
            k_range   = list(range(2, max_clusters+1))
        if(algorithm == "kmeans"):
            if metric =="silhouette":
                k_range   = list(range(2, max_clusters+1))
            if metric == "gap":
                # set number of averages to 1
                kwargs['n_init']=1
        min_k = np.min(k_range)
        target = LearningResults()

        try:
            # scale the data
            scaled_data = self.scale_data_for_clustering(
                use_decomposition_results=use_decomposition_results,
                scaling=scaling, scaling_kwargs=scaling_kwargs,
                number_pca_components=number_pca_components,
                navigation_mask=navigation_mask,
                signal_mask=signal_mask)

            # from 2 to max_clusters
            # cluster and calculate silhouette_score
            if metric == "elbow":
                pbar = progressbar(total=len(k_range))
                inertia = np.zeros(len(k_range))

                for i,k in enumerate(k_range):
                    alg = self._cluster_analysis(k,
                                                 scaled_data,
                                                 algorithm,
                                                 **kwargs)

                    D = distances_within_cluster(scaled_data,alg.labels_)
                    W = np.sum(D)
                    inertia[i]= np.log(W)
                    pbar.update(1)
                    _logger.info("For n_clusters =", k,
                                 "The distance metric is :",
                                 inertia[-1])
                    to_return = inertia
                    best_k =self.estimate_elbow_position(to_return,log=False)\
                        +min_k
            elif metric == "silhouette":
                k_range   = list(range(2, max_clusters+1))
                pbar = progressbar(total=len(k_range))
                silhouette_avg = []
                for k in k_range:
                    alg = self._cluster_analysis(k,
                                                 scaled_data,
                                                 algorithm,
                                                 **kwargs)
                    cluster_labels = alg.labels_
                    silhouette_avg.append(
                        import_sklearn.sklearn.metrics.silhouette_score(
                        scaled_data,
                        cluster_labels))
                    pbar.update(1)
                    _logger.info("For n_clusters =", k,
                                 "The average silhouette_score is :",
                                 silhouette_avg[-1])
                to_return = silhouette_avg
                best_k = []
                max_value = -1.0
                # find peaks
                for u in range(len(silhouette_avg)-1):
                    if ((silhouette_avg[u] > silhouette_avg[u-1]) &
                            (silhouette_avg[u] > silhouette_avg[u+1])):
                        best_k.append(u+min_k)
                        max_value = max(silhouette_avg[u], max_value)
                if silhouette_avg[0] > max_value:
                    best_k.insert(0, min_k)
            else:
                # cluster and calculate gap statistic
                # various empty arrays...
                reference_inertia = np.zeros(len(k_range))
                reference_std  = np.zeros(len(k_range))
                data_inertia=np.zeros(len(k_range))
                reference=np.zeros(scaled_data.shape)
                local_inertia = np.zeros(n_ref)
                pbar = progressbar(total=n_ref*len(k_range))
                # only perform 1 pass of clustering
                # otherwise std_dev isn't correct

                for o_indx,k in enumerate(k_range):
                    # calculate the data metric
                    if(algorithm=="kmeans"):
                        kwargs['n_init']=1
                    alg = self._cluster_analysis(k,
                                                 scaled_data,
                                                 algorithm,
                                                 **kwargs)

                    D = distances_within_cluster(scaled_data,alg.labels_,
                                                 squared=False)
                    W = np.sum(D)
                    data_inertia[o_indx]=np.log(W)
                    # now do n_ref clusters for a uniform random distribution
                    # to determine "gap" between data and random distribution
                    for i_indx in range(n_ref):
                        # initiate with a known seed to make the overall results
                        # repeatable but still sampling different configurations
                        for f_indx in range(scaled_data.shape[1]):
                            xmin = np.min(scaled_data[:,f_indx])
                            xmax = np.max(scaled_data[:,f_indx])
                            reference[:,f_indx]=(xmax-xmin)*\
                               np.random.random_sample(size=reference[:,0].shape)-xmin

                        alg = self._cluster_analysis(k,
                                                     reference,
                                                     algorithm,
                                                     **kwargs)
                        D = distances_within_cluster(reference,alg.labels_,
                                                 squared=False)
                        W = np.sum(D)
                        local_inertia[i_indx]=np.log(W)
                        pbar.update(1)
                    reference_inertia[o_indx]=np.mean(local_inertia)
                    reference_std[o_indx] = np.std(local_inertia)
                std_error = np.sqrt(1.0 + 1.0/n_ref)*reference_std
                std_error = np.abs(std_error)
                gap       = reference_inertia-data_inertia
                to_return = gap
                best_k = min_k
                for i in range(1,len(k_range)-1):
                    if gap[i] >= (gap[i+1]- std_error[i+1]):
                        best_k=i+min_k
                        break
        finally:
            target.cluster_metric_index      = k_range
            target.cluster_metric_data       = to_return
            target.cluster_metric            = metric
            target.number_of_clusters        = best_k
            # fold
            if self._unfolded4clustering is True:
                self.fold()
                self._unfolded4clustering = False
            self.learning_results.__dict__.update(target.__dict__)

    def estimate_elbow_position(self, explained_variance_ratio=None,log=True,
                                max_points=20):
        """Estimate the elbow position of a scree plot curve.

        Used to estimate the number of significant components in
        a PCA variance ratio plot or other "elbow" type curves.

        Find a line between first and last point on the scree plot.
        With a classic elbow scree plot, this line more or less
        defines a triangle. The elbow should be the point which
        is the furthest distance from this line. For more details,
        see [Satopää2011]_.

        Parameters
        ----------
        explained_variance_ratio : {None, numpy array}
            Explained variance ratio values that form the scree plot.
            If None, uses the ``explained_variance_ratio`` array stored
            in ``s.learning_results``, so a decomposition must have
            been performed first.
        max_points : int
            Maximum number of points to consider in the calculation.

        Returns
        -------
        elbow position : int
            Index of the elbow position in the input array. Due to
            zero-based indexing, the number of significant components
            is `elbow_position + 1`.

        References
        ----------
        .. [Satopää2011] V. Satopää, J. Albrecht, D. Irwin, and B. Raghavan.
            "Finding a “Kneedle” in a Haystack: Detecting Knee Points in
            System Behavior,. 31st International Conference on Distributed
            Computing Systems Workshops, pp. 166-171, June 2011.

        See Also
        --------
        * :py:meth:`~.learn.mva.MVA.get_explained_variance_ratio`,
        * :py:meth:`~.learn.mva.MVA.plot_explained_variance_ratio`,

        """
        if explained_variance_ratio is None:
            if self.learning_results.explained_variance_ratio is None:
                raise ValueError(
                    "A decomposition must be performed before calling "
                    "estimate_elbow_position(), or pass a numpy array directly."
                )

            curve_values = self.learning_results.explained_variance_ratio
        else:
            curve_values = explained_variance_ratio

        max_points = min(max_points, len(curve_values) - 1)
        # Clipping the curve_values from below with a v.small
        # number avoids warnings below when taking np.log(0)
        curve_values_adj = np.clip(curve_values, 1e-30, None)

        x1 = 0
        x2 = max_points

        y1 = np.log(curve_values_adj[0])
        if log:
            y2 = np.log(curve_values_adj[max_points])
        else:
            y2 = curve_values_adj[max_points]

        xs = np.arange(max_points)
        if log:
            ys = np.log(curve_values_adj[:max_points])
        else:
            ys = curve_values_adj[:max_points]

        numer = np.abs((x2 - x1) * (y1 - ys) - (x1 - xs) * (y2 - y1))
        denom = np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
        distance = np.nan_to_num(numer / denom)
        elbow_position = np.argmax(distance)

        return elbow_position


class LearningResults(object):
    """Stores the parameters and results from a decomposition."""

    # Decomposition
    factors = None
    loadings = None
    explained_variance = None
    explained_variance_ratio = None
    number_significant_components = None
    decomposition_algorithm = None
    poissonian_noise_normalized = None
    output_dimension = None
    mean = None
    centre = None
    # Clustering values
    cluster_membership = None
    cluster_labels = None
    cluster_centers = None
    cluster_algorithm = None
    number_of_clusters = None
    cluster_metric_data  = None
    cluster_metric_index = None
    cluster_metric = None
    # Unmixing
    bss_algorithm = None
    unmixing_matrix = None
    bss_factors = None
    bss_loadings = None
    # Shape
    unfolded = None
    original_shape = None
    # Masks
    navigation_mask = None
    signal_mask = None

    def save(self, filename, overwrite=None):
        """Save the result of the decomposition and demixing analysis.

        Parameters
        ----------
        filename : string
            Path to save the results to.
        overwrite : {True, False, None}, default None
            If True, overwrite the file if it exists.
            If None (default), prompt user if file exists.

        """
        kwargs = {}
        for attribute in [
            v
            for v in dir(self)
            if not isinstance(getattr(self, v), types.MethodType)
            and not v.startswith("_")
        ]:
            kwargs[attribute] = self.__getattribute__(attribute)
        # Check overwrite
        if overwrite is None:
            overwrite = io_tools.overwrite(filename)
        # Save, if all went well!
        if overwrite:
            np.savez(filename, **kwargs)
            _logger.info(f"Saved results to {filename}")

    def load(self, filename):
        """Load the results of a previous decomposition and demixing analysis.

        Parameters
        ----------
        filename : string
            Path to load the results from.

        """
        decomposition = np.load(filename, allow_pickle=True)

        for key, value in decomposition.items():
            if value.dtype == np.dtype("object"):
                value = None
            # Unwrap values stored as 0D numpy arrays to raw datatypes
            if isinstance(value, np.ndarray) and value.ndim == 0:
                value = value.item()
            setattr(self, key, value)

        _logger.info(f"Loaded results from {filename}")

        # For compatibility with old version
        if hasattr(self, "algorithm"):
            self.decomposition_algorithm = self.algorithm
            del self.algorithm
        if hasattr(self, "V"):
            self.explained_variance = self.V
            del self.V
        if hasattr(self, "w"):
            self.unmixing_matrix = self.w
            del self.w
        if hasattr(self, "variance2one"):
            del self.variance2one
        if hasattr(self, "centered"):
            del self.centered
        if hasattr(self, "pca_algorithm"):
            self.decomposition_algorithm = self.pca_algorithm
            del self.pca_algorithm
        if hasattr(self, "ica_algorithm"):
            self.bss_algorithm = self.ica_algorithm
            del self.ica_algorithm
        if hasattr(self, "v"):
            self.loadings = self.v
            del self.v
        if hasattr(self, "scores"):
            self.loadings = self.scores
            del self.scores
        if hasattr(self, "pc"):
            self.loadings = self.pc
            del self.pc
        if hasattr(self, "ica_scores"):
            self.bss_loadings = self.ica_scores
            del self.ica_scores
        if hasattr(self, "ica_factors"):
            self.bss_factors = self.ica_factors
            del self.ica_factors

        # Log summary
        self.summary()

    def __repr__(self):
        """Summarize the decomposition and demixing parameters."""
        return self.summary()

    def summary(self):
        """Summarize the decomposition and demixing parameters.

        Returns
        -------
        str
            String summarizing the learning parameters.

        """

        summary_str = (
            "Decomposition parameters\n"
            "------------------------\n"
            f"normalize_poissonian_noise={self.poissonian_noise_normalized}\n"
            f"algorithm={self.decomposition_algorithm}\n"
            f"output_dimension={self.output_dimension}\n"
            f"centre={self.centre}"
        )

        if self.bss_algorithm is not None:
            summary_str += (
                "\n\nDemixing parameters\n"
                "-------------------\n"
                f"algorithm={self.bss_algorithm}\n"
                f"n_components={len(self.unmixing_matrix)}"
            )

        _logger.info(summary_str)

        return summary_str

    def crop_decomposition_dimension(self, n, compute=False):
        """Crop the score matrix up to the given number.

        It is mainly useful to save memory and reduce the storage size

        Parameters
        ----------
        n : int
            Number of components to keep.
        compute : bool, default False
           If True and the decomposition results are lazy,
           also compute the results.

        """
        _logger.info(f"Trimming results to {n} dimensions")
        self.loadings = self.loadings[:, :n]
        if self.explained_variance is not None:
            self.explained_variance = self.explained_variance[:n]
        self.factors = self.factors[:, :n]
        if compute:
            self.loadings = self.loadings.compute()
            self.factors = self.factors.compute()
            if self.explained_variance is not None:
                self.explained_variance = self.explained_variance.compute()

    def _transpose_results(self):
        (self.factors, self.loadings, self.bss_factors, self.bss_loadings) = (
            self.loadings,
            self.factors,
            self.bss_loadings,
            self.bss_factors,
        )
        
