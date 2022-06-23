from typing import Any, Mapping, NamedTuple, Optional, Sequence, Tuple

import jax
import jax.numpy as jnp

from ott.core import fixed_point_loop, gromov_wasserstein
from ott.core.gw_barycenter.problem import GWBarycenterProblem
from ott.geometry import geometry, pointcloud


class GWBarycenterState(NamedTuple):
  """TODO.

  Attributes:
    x: Barycenter features. Only in fused case.
    c: Barycenter cost matrix.
    a: TODO.
    converged: TODO.
    errors: TODO.
    costs: TODO.
    reg_gw_cost: TODO.
  """
  x: Optional[jnp.ndarray] = None
  c: Optional[jnp.ndarray] = None
  a: Optional[jnp.ndarray] = None
  converged: bool = False
  errors: Optional[jnp.ndarray] = None
  costs: Optional[jnp.ndarray] = None
  reg_gw_cost: float = -1

  def set(self, **kwargs: Any) -> 'GWBarycenterState':
    """Return a copy of self, possibly with overwrites."""
    return self._replace(**kwargs)


@jax.tree_util.register_pytree_node_class
class GromovWassersteinBarycenter:

  def __init__(self, **kwargs: Any):
    self._quad_solver = gromov_wasserstein.GromovWasserstein(**kwargs)
    self._kwargs = kwargs
    assert not self._quad_solver.is_low_rank, "Low rank not yet implemented."

  def __call__(self, problem: GWBarycenterProblem, **kwargs: Any):
    bar_fn = jax.jit(
        iterations, static_argnums=1
    ) if self._quad_solver.jit else iterations
    state = self.init_state(problem, **kwargs)
    state = bar_fn(solver=self, problem=problem, init_state=state)
    return self.output_from_state(state)

  def init_state(
      self,
      problem: GWBarycenterProblem,
      bar_init: Tuple[jnp.ndarray, Optional[jnp.ndarray]],
      a: Optional[jnp.ndarray] = None,
  ) -> GWBarycenterState:
    """TODO.

    Args:
      problem: The barycenter problem.
      bar_init:
      a: Barycenter weights.

    Returns:
      TODO.
    """
    # TODO(michalk8): same feature initializer as in continuous barycenter?
    # TODO(michalk8): default random initializer for structure?
    c, x = bar_init
    bar_size = c.shape[0]
    if a is None:
      a = jnp.ones((bar_size,)) / bar_size

    assert c.shape == (bar_size, bar_size)
    assert a.shape == (bar_size,)
    if problem.is_fused:
      assert x is not None, "barycenter features are not initialized"
      _, _, d = problem.segmented_y_fused.shape
      assert x.shape == (bar_size, d)

    num_iter = self._quad_solver.max_iterations
    if self._quad_solver.store_inner_errors:
      errors = -jnp.ones((
          num_iter, problem.max_measure_size,
          self._quad_solver.linear_ot_solver.outer_iterations
      ))
    else:
      errors = None

    costs = -jnp.ones((num_iter,))
    return GWBarycenterState(x=x, c=c, a=a, errors=errors, costs=costs)

  def update_state(
      self,
      state: GWBarycenterState,
      iteration: int,
      problem: GWBarycenterProblem,
      store_errors: bool = True,
  ) -> Tuple[float, bool, jnp.ndarray, Optional[jnp.ndarray]]:
    from ott.core import quad_problems

    def solve_gw(
        state: GWBarycenterState, b: jnp.ndarray, y: jnp.ndarray,
        f: Optional[jnp.ndarray]
    ) -> Any:
      # TODO(michalk8): think about low rank
      geom_xx = geometry.Geometry(cost_matrix=state.c, epsilon=problem.epsilon)
      geom_yy = pointcloud.PointCloud(y, epsilon=problem.epsilon)
      if problem.is_fused:
        geom_xy = pointcloud.PointCloud(x=state.x, y=f, epsilon=problem.epsilon)
      else:
        geom_xy = None

      quad_problem = quad_problems.QuadraticProblem(
          geom_xx=geom_xx,
          geom_yy=geom_yy,
          geom_xy=geom_xy,
          a=state.a,
          b=b,
          fused_penalty=problem.fused_penalty,
      )
      out = self._quad_solver(quad_problem)

      return (
          out.reg_gw_cost, out.convergence, out.matrix,
          out.errors if store_errors else None
      )

    in_axes = [None, 0, 0]
    in_axes += [0] if problem.is_fused else [None]
    solve_fn = jax.vmap(solve_gw, in_axes=in_axes)

    y, b = problem.segmented_y_b
    y_f = problem.segmented_y_fused
    costs, convs, transports, errors = solve_fn(state, b, y, y_f)

    cost = jnp.sum(costs * problem.weights)
    costs = state.costs.at[iteration].set(cost)

    x_new = problem.update_features(transports, state.a)
    c_new = problem.update_barycenter(transports, state.a)
    # TODO(michalk8): set other flags

    return state.set(x=x_new, c=c_new, costs=costs)

  def output_from_state(self, state: GWBarycenterState) -> GWBarycenterState:
    # for consistency with cont. barycenter, will be refactored in the future
    return state

  def tree_flatten(self) -> Tuple[Sequence[Any], Mapping[str, Any]]:
    return [], self._kwargs

  @classmethod
  def tree_unflatten(
      cls, aux_data: Mapping[str, Any], children: Sequence[Any]
  ) -> "GromovWassersteinBarycenter":
    del children
    return cls(**aux_data)


def iterations(
    solver: GromovWassersteinBarycenter, problem: GWBarycenterProblem,
    init_state: GWBarycenterState
) -> GWBarycenterState:

  def cond_fn(
      iteration: int, constants: GromovWassersteinBarycenter,
      state: GWBarycenterState
  ) -> bool:
    solver, _ = constants
    return solver._quad_solver._continue(state, iteration)

  def body_fn(
      iteration, constants: Tuple[GromovWassersteinBarycenter,
                                  GWBarycenterProblem],
      state: GWBarycenterState, compute_error: bool
  ) -> GWBarycenterState:
    del compute_error  # always assumed true
    solver, problem = constants
    return solver.update_state(state, iteration, problem)

  state = fixed_point_loop.fixpoint_iter(
      cond_fn=cond_fn,
      body_fn=body_fn,
      min_iterations=solver._quad_solver.min_iterations,
      max_iterations=solver._quad_solver.max_iterations,
      inner_iterations=1,
      constants=(solver, problem),
      state=init_state,
  )
  return state