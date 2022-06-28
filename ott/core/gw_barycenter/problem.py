from functools import partial
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

import jax
import jax.numpy as jnp
from typing_extensions import Literal

from ott.core import bar_problems, continuous_barycenter, quad_problems, segment
from ott.geometry import costs, geometry, pointcloud


@jax.tree_util.register_pytree_node_class
class GWBarycenterProblem(bar_problems.BarycenterProblem):

  def __init__(
      self,
      y: Optional[jnp.ndarray] = None,
      b: Optional[jnp.ndarray] = None,
      y_fused: Optional[jnp.ndarray] = None,
      fused_penalty: float = 1.0,
      loss: Literal['sqeucl', 'kl'] = 'sqeucl',
      scale_cost: Optional[Union[float, Literal["mean", "max_cost"]]] = None,
      is_cost: bool = False,
      **kwargs: Any,
  ):
    """Gromov-Wasserstein barycenter problem, possibly fused.

    Args:
      y: Array of shape ``[num_measures, N, D]`` containing all points.
        If ``is_cost = True``, each measure will be interpreted as
        a cost matrix, otherwise as a point cloud.
        Alternatively, array of shape ``[num_total_points, D]`` containing all
        measures can be used that will be reshaped to ``[num_measures, N, D]``.
        See :func:`ott.core.segment.segment_point_cloud`.
      b: Array of shape ``[num_measures, N]`` containing the weights
        (within each measure) of all the points.
      y_fused: Array of shape ``[num_measures, N, D_f]`` containing the features
        of all points used to define the linear term in the fused case.
      loss: Gromov-Wasserstein loss.
      fused_penalty: Multiplier of the linear term in Fused Gromov-Wasserstein.
        Only used when ``y_fused != None``.
      scale_cost: Scaling passed to geometries.
      is_cost: Whether ``y`` represents cost matrices or point clouds.
      kwargs: Keyword arguments for
        :class:`ott.core.bar_problems.BarycenterProblem`.
    """
    super().__init__(y=y, b=b, **kwargs)
    self._y_fused = y_fused
    self.fused_penalty = fused_penalty
    self.loss, self._loss_name = self._create_loss(loss), loss
    self.scale_cost = scale_cost
    self.is_cost = is_cost

  def update_barycenter(
      self, transports: jnp.ndarray, a: jnp.ndarray
  ) -> jnp.ndarray:
    """Update the barycenter cost matrix.

    Args:
      transports: Transport maps of shape ``[num_measures, N, M]``.
      a: Barycenter weights of shape ``[N,]``.

    Returns:
      Cost matrix of shape ``[N, N]``.
    """

    @partial(jax.vmap, in_axes=[0, 0, None])
    def project(
        y: jnp.ndarray, transport: jnp.ndarray,
        fn: Optional[Callable[[jnp.ndarray], jnp.ndarray]]
    ) -> jnp.ndarray:
      if self.is_cost:
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
    barycenter *= 1. / jnp.vdot(a, a)

    if self._loss_name == 'kl':
      barycenter = jnp.exp(barycenter)
    return barycenter

  def update_features(self, transports: jnp.ndarray,
                      a: jnp.ndarray) -> Optional[jnp.ndarray]:
    if not self.is_fused:
      return None

    y_fused = self.segmented_y_fused
    weights = self.weights[:, None, None]

    if self._loss_name == "sqeucl":
      cost = costs.Euclidean()
      divide_a = jnp.where(a > 0, 1.0 / a, 1.0)
      transports = transports * divide_a[None, :, None]
      return jnp.sum(
          weights * continuous_barycenter
          .barycentric_projection(transports, y_fused, cost),
          axis=0
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
    children, aux = super().tree_flatten()
    aux["y_fused"] = self._y_fused
    aux['fused_penalty'] = self.fused_penalty
    aux['loss'] = self._loss_name
    aux['scale_cost'] = self.scale_cost
    aux['is_cost'] = self.is_cost
    return children, aux
