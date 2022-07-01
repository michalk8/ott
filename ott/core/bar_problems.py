# Copyright 2022 Apple Inc
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Classes defining OT problem(s) (objective function + utilities)."""
import functools
from functools import partial
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

import jax
import jax.numpy as jnp
from typing_extensions import Literal

from ott.core import quad_problems, segment
from ott.geometry import costs, geometry, pointcloud

__all__ = ["BarycenterProblem", "GWBarycenterProblem", "barycentric_projection"]


@jax.tree_util.register_pytree_node_class
class BarycenterProblem:
  """Definition of a linear regularized OT problem and some tools.

  Args:
    y: a matrix merging the points of all measures.
    b: a vector containing the weights (within each masure) of all the points.
    weights: weights of the barycenter problem (size num_segments).
    cost_fn: cost function used.
    epsilon: epsilon regularization used to solve reg-OT problems.
    debiased: whether the problem is debiased, in the sense that
      the regularized transportation cost of barycenter to itself will
      be considered when computing gradient. Note that if the debiased option
      is used, the barycenter size (used in call function) needs to be smaller
      than the max_measure_size parameter below, for parallelization to
      operate efficiently.
      Currently not implemented.
    segment_ids: describe for each point to which measure it belongs.
    num_segments: total number of measures
    indices_are_sorted: flag indicating indices in segment_ids are sorted.
    num_per_segment: number of points in each segment, if contiguous.
    max_measure_size: max number of points in each segment (for efficient jit)
  """

  def __init__(
      self,
      y: jnp.ndarray,
      b: Optional[jnp.ndarray] = None,
      weights: Optional[jnp.ndarray] = None,
      cost_fn: Optional[costs.CostFn] = None,
      epsilon: Optional[jnp.ndarray] = None,
      debiased: bool = False,
      segment_ids: Optional[jnp.ndarray] = None,
      num_segments: Optional[jnp.ndarray] = None,
      indices_are_sorted: Optional[bool] = None,
      num_per_segment: Optional[jnp.ndarray] = None,
      max_measure_size: Optional[int] = None
  ):
    self._y = y
    self._b = b
    self._weights = weights
    self.cost_fn = costs.Euclidean() if cost_fn is None else cost_fn
    self.epsilon = epsilon
    self.debiased = debiased
    self._segment_ids = segment_ids
    self._num_segments = num_segments
    self._indices_are_sorted = indices_are_sorted
    self._num_per_segment = num_per_segment
    self._max_measure_size = max_measure_size

  def tree_flatten(self):
    return ([self._y, self._b, self._weights], {
        'cost_fn': self.cost_fn,
        'epsilon': self.epsilon,
        'debiased': self.debiased,
        'segment_ids': self._segment_ids,
        'num_segments': self._num_segments,
        'indices_are_sorted': self._indices_are_sorted,
        'num_per_segment': self._num_per_segment,
        'max_measure_size': self._max_measure_size
    })

  @classmethod
  def tree_unflatten(cls, aux_data, children):
    return cls(*children, **aux_data)

  @property
  def segmented_y_b(
      self
  ) -> Tuple[Optional[jnp.ndarray], Optional[jnp.ndarray]]:
    if self._y is None or (self._y.ndim == 3 and self._b.ndim == 2):
      return self.add_slice_for_debiased(self._y, self._b)
    else:
      segmented_y, segmented_b, _ = segment.segment_point_cloud(
          self._y, self._b, self._segment_ids, self._num_segments,
          self._indices_are_sorted, self._num_per_segment, self.max_measure_size
      )
    return self.add_slice_for_debiased(segmented_y, segmented_b)

  def add_slice_for_debiased(
      self, y: Optional[jnp.ndarray], b: Optional[jnp.ndarray]
  ) -> Tuple[Optional[jnp.ndarray], Optional[jnp.ndarray]]:
    if y is None or b is None:
      return y, b
    if self.debiased:
      n, dim = y.shape[1], y.shape[2]
      y = jnp.concatenate((y, jnp.zeros((1, n, dim))), axis=0)
      b = jnp.concatenate((b, jnp.zeros((
          1,
          n,
      ))), axis=0)
    return y, b

  @property
  def flattened_y(self) -> Optional[jnp.ndarray]:
    """Array of shape ``[num_measures * N, D]``."""
    if self._y is not None and self._y.ndim == 3:
      return self._y.reshape((-1, self._y.shape[-1]))
    else:
      return self._y

  @property
  def flattened_b(self) -> Optional[jnp.ndarray]:
    """Array of shape ``[num_measures * N,]``."""
    if self._b is not None and self._b.ndim == 2:
      return self._b.ravel()
    else:
      return self._b

  @property
  def max_measure_size(self) -> int:
    """Maximum number of points across all measures."""
    if self._max_measure_size is not None:
      return self._max_measure_size
    if self._y is not None and self._y.ndim == 3:
      return self._y.shape[1]
    else:
      if self._num_per_segment is None:
        num_segments = self._num_segments
        indices_are_sorted = self._indices_are_sorted

        if num_segments is None:
          num_segments = jnp.max(self._segment_ids) + 1
        if indices_are_sorted is None:
          indices_are_sorted = False

        num_per_segment = jax.ops.segment_sum(
            jnp.ones_like(self._segment_ids),
            self._segment_ids,
            num_segments=num_segments,
            indices_are_sorted=indices_are_sorted
        )
        return jnp.max(num_per_segment)
      else:
        return jnp.max(self._num_per_segment)

  @property
  def num_segments(self) -> int:
    """Number of measures."""
    if self._y is None:
      return 0
    if self._y.ndim == 3:
      if self._b is not None:
        assert self._y.shape[0] == self._b.shape[0]
      return self._y.shape[0]
    else:
      _, _, num_segments = segment.segment_point_cloud(
          self._y, self._b, self._segment_ids, self._num_segments,
          self._indices_are_sorted, self._num_per_segment, self.max_measure_size
      )
    return num_segments

  @property
  def weights(self) -> jnp.ndarray:
    """Array of shape ``[num_measures,]`` that sums to 1."""
    if self._weights is None:
      weights = jnp.ones((self.num_segments,)) / self.num_segments
    else:
      # Check that the number of measures coincides with the weights' size.
      assert self._weights.shape[0] == self.num_segments
      # By default, we assume that weights sum to 1, and enforce this if needed.
      weights = self._weights / jnp.sum(self._weights)
    if self.debiased:
      weights = jnp.concatenate((weights, jnp.array([-0.5])))
    return weights


# TODO(michalk8): add citations
@jax.tree_util.register_pytree_node_class
class GWBarycenterProblem(BarycenterProblem):
  """Gromov-Wasserstein barycenter problem, possibly fused.

  Args:
    y: Array of shape ``[num_measures, N, D]`` containing all points as point
      clouds. Alternatively, stacked array of shape ``[num_total_points, D]``
      can also be specified that will be reshaped to ``[num_measures, N, D]``
      where ``N`` larger or equal to the maximum number of points within all
      measures. See :class:`~ott.core.bar_problems.BarycenterProblem` or
      :func:`~ott.core.segment.segment_point_cloud` for more information.
    b: Array of shape ``[num_measures, N]`` containing the weights
      (within each measure) of all the points.
    weights: weights of the barycenter problem (size num_segments).
    cost: Alternative to ``y``, an array of shape ``[num_measures, N, N]`` that
      defines padded cost matrices for each measure. Only one of ``y`` and
      ``cost`` can be passed. See :func:`ott.core.segment.pad_along_axis`
      on how to pad cost matrices of different sized.
    y_fused: Array of shape ``[num_measures, N, D_f]`` containing the features
      of all points used to define the linear term in the fused case.
      Similarly to ``y``, can be specified as a stacked array of shape
      ``[num_total_points, D_f]``.
    loss: Gromov-Wasserstein loss.
    fused_penalty: Multiplier of the linear term in Fused Gromov-Wasserstein.
      Only used when ``y_fused != None``.
    scale_cost: Scaling passed to geometries.
    kwargs: Keyword arguments for
      :class:`ott.core.bar_problems.BarycenterProblem`.
  """

  def __init__(
      self,
      y: Optional[jnp.ndarray] = None,
      b: Optional[jnp.ndarray] = None,
      weights: Optional[jnp.ndarray] = None,
      cost: Optional[jnp.ndarray] = None,
      y_fused: Optional[jnp.ndarray] = None,
      fused_penalty: float = 1.0,
      loss: Literal['sqeucl', 'kl'] = 'sqeucl',
      scale_cost: Optional[Union[float, Literal["mean", "max_cost"]]] = None,
      **kwargs: Any,
  ):
    assert y is None or cost is None, "Cannot specify both `y` and `cost`."
    super().__init__(y if cost is None else cost, b, weights, **kwargs)
    self._is_cost = cost is not None
    self._y_fused = y_fused
    self.fused_penalty = fused_penalty
    self.loss, self._loss_name = self._create_loss(loss), loss
    self.scale_cost = scale_cost

  def update_barycenter(
      self, transports: jnp.ndarray, a: jnp.ndarray
  ) -> jnp.ndarray:
    """Update the barycenter cost matrix.

    Args:
      transports: Transport maps of shape ``[num_measures, B, N]``.
      a: Barycenter weights of shape ``[B,]``.

    Returns:
      Cost matrix of shape ``[B, B]``.
    """

    @partial(jax.vmap, in_axes=[0, 0, None])
    def project(
        y: jnp.ndarray, transport: jnp.ndarray,
        fn: Optional[Callable[[jnp.ndarray], jnp.ndarray]]
    ) -> jnp.ndarray:
      if self._is_cost:
        assert y.shape[0] == y.shape[1], y.shape
        geom = geometry.Geometry(
            y, epsilon=self.epsilon, scale_cost=self.scale_cost
        )
      else:
        geom = pointcloud.PointCloud(
            y,
            cost_fn=self.cost_fn,
            epsilon=self.epsilon,
            scale_cost=self.scale_cost
        )
      tmp = geom.apply_cost(transport.T, axis=0, fn=fn)
      return transport @ tmp

    fn = None if self._loss_name == 'sqeucl' else self.loss[1][1]
    y, _ = self.segmented_y_b
    weights = self.weights[:, None, None]

    barycenter = jnp.sum(weights * project(y, transports, fn), axis=0)
    # TODO(michalk8): more efficient impl.
    barycenter /= jnp.outer(a, a)

    if self._loss_name == 'kl':
      barycenter = jnp.exp(barycenter)
    return barycenter

  def update_features(self, transports: jnp.ndarray,
                      a: jnp.ndarray) -> Optional[jnp.ndarray]:
    """Update the barycenter features. Only used in the fused cased.

    Only implemented for :class:`~ott.geometry.costs.Euclidean` cost.

    Args:
      transports: Transport maps of shape ``[num_measures, N, M]``.
      a: Barycenter weights of shape ``[N,]``.

    Returns:
      Array of shape ``[N, D_f]`` containing the update features.
    """
    if not self.is_fused:
      return None

    y_fused = self.segmented_y_fused
    weights = self.weights[:, None, None]
    divide_a = jnp.where(a > 0, 1.0 / a, 1.0)
    transports = transports * divide_a[None, :, None]

    if self._loss_name == "sqeucl":
      cost = costs.Euclidean()
      return jnp.sum(
          weights * barycentric_projection(transports, y_fused, cost), axis=0
      )
    raise NotImplementedError(self._loss_name)

  @property
  def is_fused(self) -> bool:
    """Whether this problem is fused."""
    return self._y_fused is not None

  @property
  def segmented_y_fused(self) -> Optional[jnp.ndarray]:
    """Array of shape ``[num_measures, N, D_f]`` used in the fused case."""
    if self._y_fused is None or self._y_fused.ndim == 3:
      return self._y_fused
    segmented_y_fused, _, _ = segment.segment_point_cloud(
        self._y_fused, None, self._segment_ids, self._num_segments,
        self._indices_are_sorted, self._num_per_segment, self.max_measure_size
    )
    return segmented_y_fused

  @staticmethod
  def _create_loss(loss: Literal['sqeucl', 'kl']) -> quad_problems.Loss:
    # TODO(michalk8): use namedtuple for in `quad_problems`
    if loss == 'sqeucl':
      return quad_problems.make_square_loss()
    if loss == 'kl':
      return quad_problems.make_kl_loss()
    raise NotImplementedError(f"Loss `{loss}` is not yet implemented.")

  def tree_flatten(self) -> Tuple[Sequence[Any], Dict[str, Any]]:
    (y, b, weights), aux = super().tree_flatten()
    if self._is_cost:
      children = [None, b, weights, y]
    else:
      children = [y, b, weights, None]
    aux["y_fused"] = self._y_fused
    aux['fused_penalty'] = self.fused_penalty
    aux['loss'] = self._loss_name
    aux['scale_cost'] = self.scale_cost
    return children, aux


@functools.partial(jax.vmap, in_axes=[0, 0, None])
def barycentric_projection(
    matrix: jnp.ndarray, y: jnp.ndarray, cost_fn
) -> jnp.ndarray:
  return jax.vmap(cost_fn.barycenter, in_axes=[0, None])(matrix, y)
