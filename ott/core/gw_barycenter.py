from functools import partial
from types import MappingProxyType
from typing import Any, Dict, Mapping, NamedTuple, Optional, Sequence, Tuple, Union

import jax
import jax.numpy as jnp

from ott.core import (
    fixed_point_loop,
    gromov_wasserstein,
    problems,
    quad_problems,
    was_solver,
)
from ott.core.bar_problems import GWBarycenterProblem
from ott.geometry import geometry, pointcloud

__all__ = ["GWBarycenterState", "GromovWassersteinBarycenter"]


class GWBarycenterState(NamedTuple):
  """Holds the state of the :class:`ott.core.bar_problems.GWBarycenterProblem`.

  Args:
    x: Barycenter features of shape ``[B, D_f]``. Only used in the fused case.
    c: Barycenter cost matrix of shape ``[B, B]``.
    a: Weights of the barycenter of shape ``[B,]``.
    errors: Array of shape
      ``[max_iter, num_measures, quad_max_iter, lin_outer_iter]`` containing
      the GW errors at each iteration.
    costs: Array of shape ``[max_iter,]`` containing the cost at each iteration.
    gw_convergence: Array of shape ``[max_iter,]`` containing the convergence
      of all GW problems at each iteration.
  """
  x: Optional[jnp.ndarray] = None
  c: Optional[jnp.ndarray] = None
  a: Optional[jnp.ndarray] = None
  errors: Optional[jnp.ndarray] = None
  costs: Optional[jnp.ndarray] = None
  gw_convergence: Optional[jnp.ndarray] = None

  def set(self, **kwargs: Any) -> 'GWBarycenterState':
    """Return a copy of self, possibly with overwrites."""
    return self._replace(**kwargs)


# TODO(michalk8): add citations
@jax.tree_util.register_pytree_node_class
class GromovWassersteinBarycenter(was_solver.WassersteinSolver):
  """Gromov-Wasserstein barycenter solver.

  Args:
    epsilon: Entropy regulariser.
    min_iterations: Minimum number of iterations.
    max_iterations: Maximum number of outermost iterations.
    threshold: Convergence threshold.
    jit: Whether to jit the iteration loop.
    store_inner_errors: Whether to store the iterations of inner GW solver.
    gw_kwargs: Keyword argument for
      :class:`ott.core.gromov_wasserstein.GromovWasserstein`.
  """

  def __init__(
      self,
      epsilon: Optional[float] = None,
      min_iterations: int = 5,
      max_iterations: int = 50,
      threshold: float = 1e-3,
      jit: bool = True,
      store_inner_errors: bool = False,
      gw_kwargs: Mapping[str, Any] = MappingProxyType({}),
  ):
    super().__init__(
        epsilon=epsilon,
        min_iterations=min_iterations,
        max_iterations=max_iterations,
        threshold=threshold,
        jit=jit,
        store_inner_errors=store_inner_errors
    )
    gw_kwargs = dict(gw_kwargs)
    gw_kwargs["epsilon"] = epsilon
    # TODO(michalk8): store only GW errors?
    # would be ``[max_iter, num_measures, num_gw_iters]``
    gw_kwargs["store_inner_errors"] = store_inner_errors
    self._quad_solver = gromov_wasserstein.GromovWasserstein(**gw_kwargs)
    assert not self._quad_solver.is_low_rank, "Low rank not yet implemented."

  def __call__(self, problem: GWBarycenterProblem, **kwargs: Any):
    bar_fn = jax.jit(iterations, static_argnums=1) if self.jit else iterations
    state = self.init_state(problem, **kwargs)
    state = bar_fn(solver=self, problem=problem, init_state=state)
    return self.output_from_state(state)

  def init_state(
      self,
      problem: GWBarycenterProblem,
      bar_init: Union[int, jnp.ndarray, Tuple[jnp.ndarray, jnp.ndarray]],
      a: Optional[jnp.ndarray] = None,
      seed: int = 0,
  ) -> GWBarycenterState:
    """Initialize the (fused) Gromov-Wasserstein barycenter state.

    Args:
      problem: The barycenter problem.
      bar_init: Initial barycenter value. Can be one of following:

        - :class:`int` - randomly initialize barycenter, see also ``seed``.
        - :class:`jax.numpy.ndarray` - barycenter cost matrix ``[B, B]``.
          Only used in the non-fused case.
        - 2- :class:`tuple` of :class:`jax.numpy.ndarray` - the 1st array
          corresponds to ``[B, B]`` cost matrix, the 2nd array is ``[B, D_f]``
          barycenter feature array. Only used in the fused case.

      a: An array of shape ``[B,]`` containing the barycenter weights.
      seed: Random seed when ``bar_init`` is :class:`int`.

    Returns:
      The initial barycenter state.
    """
    if isinstance(bar_init, int):
      if a is None:
        a = jnp.ones((bar_init,)) / bar_init

      rng = jax.random.PRNGKey(seed)
      _, b = problem.segmented_y_b
      keys = jax.random.split(rng, len(b))

      linear_solver = self._quad_solver.linear_ot_solver
      transports = init_transports(linear_solver, keys, a, b, problem.epsilon)

      x = problem.update_features(transports, a)
      c = problem.update_barycenter(transports, a)
    else:
      c, x = bar_init if isinstance(bar_init, tuple) else (bar_init, None)
      bar_size = c.shape[0]

      if a is None:
        a = jnp.ones((bar_size,)) / bar_size
      assert c.shape == (bar_size, bar_size)
      assert a.shape == (bar_size,)

      if problem.is_fused:
        assert x is not None, "barycenter features are not initialized"
        _, _, d = problem.segmented_y_fused.shape
        assert x.shape == (bar_size, d)

    num_iter = self.max_iterations
    if self.store_inner_errors:
      # TODO(michalk8): in the future, think about how to do this in general
      errors = -jnp.ones((
          num_iter, problem.num_segments, self._quad_solver.max_iterations,
          self._quad_solver.linear_ot_solver.outer_iterations
      ))
    else:
      errors = None

    costs = -jnp.ones((num_iter,))
    gw_convergence = -jnp.ones((num_iter,))
    return GWBarycenterState(
        x=x,
        c=c,
        a=a,
        errors=errors,
        costs=costs,
        gw_convergence=gw_convergence
    )

  def update_state(
      self,
      state: GWBarycenterState,
      iteration: int,
      problem: GWBarycenterProblem,
      store_errors: bool = True,
  ) -> Tuple[float, bool, jnp.ndarray, Optional[jnp.ndarray]]:

    def solve_gw(
        state: GWBarycenterState, b: jnp.ndarray, y: jnp.ndarray,
        f: Optional[jnp.ndarray]
    ) -> Tuple[float, bool, jnp.ndarray, Optional[jnp.ndarray]]:
      eps, scale, cost_fn = problem.epsilon, problem.scale_cost, problem.cost_fn

      geom_xx = geometry.Geometry(state.c, epsilon=eps, scale_cost=scale)
      if problem.is_cost:
        geom_yy = geometry.Geometry(y, epsilon=eps, scale_cost=scale)
      else:
        geom_yy = pointcloud.PointCloud(
            y, cost_fn=cost_fn, epsilon=eps, scale_cost=scale
        )
      if problem.is_fused:
        geom_xy = pointcloud.PointCloud(
            state.x, f, cost_fn=cost_fn, epsilon=eps, scale_cost=scale
        )
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
    costs, convergeds, transports, errors = solve_fn(state, b, y, y_f)

    cost = jnp.sum(costs * problem.weights)
    costs = state.costs.at[iteration].set(cost)
    converged = jnp.all(convergeds)
    gw_convergence = state.gw_convergence.at[iteration].set(converged)

    if self.store_inner_errors:
      errors = state.errors.at[iteration, ...].set(errors)
    else:
      errors = None

    x_new = problem.update_features(transports, state.a)
    c_new = problem.update_barycenter(transports, state.a)
    return state.set(
        x=x_new,
        c=c_new,
        costs=costs,
        errors=errors,
        gw_convergence=gw_convergence
    )

  def output_from_state(self, state: GWBarycenterState) -> GWBarycenterState:
    """No-op."""
    # TODO(michalk8): just for consistency with continuous barycenter
    # will be refactored in the future
    return state

  def tree_flatten(self) -> Tuple[Sequence[Any], Dict[str, Any]]:
    children, aux = super().tree_flatten()
    aux['_gw_kwargs'] = self._quad_solver._kwargs
    return children, aux

  @classmethod
  def tree_unflatten(
      cls, aux_data: Dict[str, Any], children: Sequence[Any]
  ) -> "GromovWassersteinBarycenter":
    epsilon, _, _, threshold = children
    gw_kwargs = aux_data.pop("_gw_kwargs")
    return cls(
        epsilon=epsilon,
        threshold=threshold,
        gw_kwargs=gw_kwargs,
        **aux_data,
    )


@partial(jax.vmap, in_axes=[None, 0, None, 0, None])
def init_transports(
    solver, key: jnp.ndarray, a: jnp.ndarray, b: jnp.ndarray,
    epsilon: Optional[float]
) -> jnp.ndarray:
  """Initialize random cost matrix and solve the OT problem.

  Args:
    solver: Linear OT solver.
    key: Random key.
    a: Source marginals (e.g., for barycenter) of shape ``[B,]``.
    b: Target marginals of shape ``[N,]``.
    epsilon: Entropy regularization.

  Returns:
    Transport map of shape ``[B, N]``.
  """
  cost = jax.random.uniform(key, shape=(len(a), len(b)), minval=0, maxval=1)
  geom = geometry.Geometry(cost, epsilon=epsilon)
  problem = problems.LinearProblem(geom, a=a, b=b)
  return solver(problem).matrix


def iterations(
    solver: GromovWassersteinBarycenter, problem: GWBarycenterProblem,
    init_state: GWBarycenterState
) -> GWBarycenterState:

  def cond_fn(
      iteration: int, constants: GromovWassersteinBarycenter,
      state: GWBarycenterState
  ) -> bool:
    solver, _ = constants
    return solver._continue(state, iteration)

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
      min_iterations=solver.min_iterations,
      max_iterations=solver.max_iterations,
      inner_iterations=1,
      constants=(solver, problem),
      state=init_state,
  )
  return state
