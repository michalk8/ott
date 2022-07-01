from functools import partial
from typing import Any, Dict, NamedTuple, Optional, Sequence, Tuple, Union

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
    c: Barycenter cost matrix of shape ``[k, k]``.
    x: Barycenter features of shape ``[k, D_f]``. Only used in the fused case.
    a: Weights of the barycenter of shape ``[k,]``.
    errors: Array of shape
      ``[max_iter, num_measures, quad_max_iter, lin_outer_iter]`` containing
      the GW errors at each iteration.
    costs: Array of shape ``[max_iter,]`` containing the cost at each iteration.
    gw_convergence: Array of shape ``[max_iter,]`` containing the convergence
      of all GW problems at each iteration.
  """
  cost: Optional[jnp.ndarray] = None
  x: Optional[jnp.ndarray] = None
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
    store_inner_errors: Whether to store the errors of the GW solver, as well
      as its linear solver, at each iteration for each measure.
    quad_solver: The GW solver.
    kwargs: Keyword argument for
      :class:`ott.core.gromov_wasserstein.GromovWasserstein`.
      Only used when ``quad_solver = None``.
  """

  def __init__(
      self,
      epsilon: Optional[float] = None,
      min_iterations: int = 5,
      max_iterations: int = 50,
      threshold: float = 1e-3,
      jit: bool = True,
      store_inner_errors: bool = False,
      quad_solver: Optional[gromov_wasserstein.GromovWasserstein] = None,
      # TODO(michalk8): this maintains the API compatibility with `was_solver`
      # but makes passing kwargs with the same name to `quad_solver` impossible
      # will be fixed when refactoring the solvers
      # note that `was_solver` also suffers from this
      **kwargs: Any,
  ):
    super().__init__(
        epsilon=epsilon,
        min_iterations=min_iterations,
        max_iterations=max_iterations,
        threshold=threshold,
        jit=jit,
        store_inner_errors=store_inner_errors
    )
    self._quad_solver = quad_solver
    if quad_solver is None:
      kwargs["epsilon"] = epsilon
      # TODO(michalk8): store only GW errors?
      kwargs["store_inner_errors"] = store_inner_errors
      self._quad_solver = gromov_wasserstein.GromovWasserstein(**kwargs)
    assert not self._quad_solver.is_low_rank, "Low rank is not yet implemented."

  def __call__(self, problem: GWBarycenterProblem, **kwargs: Any):
    bar_fn = jax.jit(iterations, static_argnums=1) if self.jit else iterations
    state = self.init_state(problem, **kwargs)
    state = bar_fn(solver=self, problem=problem, init_state=state)
    return self.output_from_state(state)

  def init_state(
      self,
      problem: GWBarycenterProblem,
      bar_size: int,
      bar_init: Optional[Union[jnp.ndarray, Tuple[jnp.ndarray,
                                                  jnp.ndarray]]] = None,
      a: Optional[jnp.ndarray] = None,
      seed: int = 0,
  ) -> GWBarycenterState:
    """Initialize the (fused) Gromov-Wasserstein barycenter state.

    Args:
      problem: The barycenter problem.
      bar_size: Size of the barycenter.
      bar_init: Initial barycenter value. Can be one of following:

        - ``None`` - randomly initialize the barycenter, see also ``seed``.
        - :class:`jax.numpy.ndarray` - barycenter cost matrix ``[k, k]``.
          Only used in the non-fused case.
        - 2- :class:`tuple` of :class:`jax.numpy.ndarray` - the 1st array
          corresponds to ``[k, k]`` cost matrix, the 2nd array is ``[k, D_f]``
          barycenter feature array. Only used in the fused case.

      a: An array of shape ``[k,]`` containing the barycenter weights.
      seed: Random seed used when ``bar_init = None``.

    Returns:
      The initial barycenter state.
    """
    if a is None:
      a = jnp.ones((bar_size,)) / bar_size
    else:
      assert a.shape == (bar_size,)

    if bar_init is None:
      _, b = problem.segmented_y_b
      rng = jax.random.PRNGKey(seed)
      keys = jax.random.split(rng, problem.num_segments)
      linear_solver = self._quad_solver.linear_ot_solver

      transports = init_transports(linear_solver, keys, a, b, problem.epsilon)
      x = problem.update_features(transports, a)
      cost = problem.update_barycenter(transports, a)
    else:
      cost, x = bar_init if isinstance(bar_init, tuple) else (bar_init, None)
      assert cost.shape == (bar_size, bar_size)
      if problem.is_fused:
        assert x is not None, "Barycenter features are not initialized."
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
        cost=cost,
        x=x,
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

      geom_xx = geometry.Geometry(state.cost, epsilon=eps, scale_cost=scale)
      if problem._y_as_costs:
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
    cost_new = problem.update_barycenter(transports, state.a)
    return state.set(
        cost=cost_new,
        x=x_new,
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
    aux["quad_solver"] = self._quad_solver
    return children, aux

  @classmethod
  def tree_unflatten(
      cls, aux_data: Dict[str, Any], children: Sequence[Any]
  ) -> "GromovWassersteinBarycenter":
    epsilon, _, _, threshold = children
    return cls(
        epsilon=epsilon,
        threshold=threshold,
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
    a: Source marginals (e.g., for barycenter) of shape ``[k,]``.
    b: Target marginals of shape ``[N,]``.
    epsilon: Entropy regularization.

  Returns:
    Transport map of shape ``[k, N]``.
  """
  key1, key2 = jax.random.split(key, 2)
  x = jax.random.normal(key1, shape=(len(a), 2))
  y = jax.random.normal(key2, shape=(len(b), 2))
  geom = pointcloud.PointCloud(x, y, epsilon=epsilon)
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
