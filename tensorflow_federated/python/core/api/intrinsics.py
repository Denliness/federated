# Copyright 2018, The TensorFlow Federated Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Defines intrinsics for use in composing federated computations."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensorflow_federated.python.common_libs import py_typecheck

from tensorflow_federated.python.core.api import placements
from tensorflow_federated.python.core.api import types
from tensorflow_federated.python.core.api import value_base

from tensorflow_federated.python.core.impl import computation_building_blocks
from tensorflow_federated.python.core.impl import intrinsic_defs
from tensorflow_federated.python.core.impl import type_constructors
from tensorflow_federated.python.core.impl import type_utils
from tensorflow_federated.python.core.impl import value_impl


def federated_aggregate(value, zero, accumulate, merge, report):
  """Aggregates `value` from `CLIENTS` to `SERVER` using a multi-stage process.

  This generalized aggregation function admits multi-layered architectures that
  involve one or more intermediate stages to handle scalable aggregation across
  a very large number of participants.

  The aggregation process is defined as follows:

  * Clients are organized into groups. Within each group, a set of all the
    member constituents of `value` contributed by clients in the group are first
    reduced in a manner similar to `federated_reduce` using reduction operator
    `accumulate` with `zero` as the zero in the algebra. As described in the
    documentation for `federated_reduce`, if members of `value` are of type `T`,
    and `zero` (the result of reducing an empty set) is of type `U`, the
    reduction operator `accumulate` used at this stage should be of type
    `(<U,T> -> U)`. The result of this stage is a set of items of type `U`, one
    item for each group of clients.

  * Next, the `U`-typed items generated by the preceding stage are merged using
    the binary commutative associative operator `merge` of type `(<U,U> -> U)`.
    This can be interpreted as a `federated_reduce` using `merge` as the
    reduction operator, and the same `zero` in the algebra. The result of this
    stage is a single top-level `U` that emerges at the root of the hierarchy at
    the `SERVER`. Actual implementations may structure this step as cascade of
    multiple layers.

  * Finally, the `U`-typed result of the reduction performed in the preceding
    stage is projected into the result value using `report` as the mapping
    function (for example, if the structures being merged consist of counters,
    this final step might include computing their ratios).

  Args:
    value: A value of a TFF federated type placed at `CLIENTS` to aggregate.
    zero: The zero in the algebra of reduction operators, as described above.
    accumulate: The reduction operator to use in the first stage of the process.
      If `value` is of type `{T}@CLIENTS`, and `zero` is of type `U`, this
      operator should be of type `(<U,T> -> U)`.
    merge: The reduction operator to employ in the second stage of the process.
      Must be of type `(<U,T> -> U)`, where `T` and `U` are as defined above.
    report: The projection operator to use at the final stage of the process to
      compute the final resulrt of aggregation. If the indended result to be
      returned by `federated_aggregate` is of type `R@SERVER`, this operator
      must be of type `(U -> R)`.

  Returns:
    A representation on the `SERVER` of the result of aggregating `value` using
    the multi-stage process described above.

  Raises:
    TypeError: if the arguments are not of the types specified above.
  """
  value = value_impl.to_value(value)
  type_utils.check_federated_value_placement(value, placements.CLIENTS,
                                             'value to be aggregated')

  zero = value_impl.to_value(zero)
  py_typecheck.check_type(zero, value_base.Value)

  # TODO(b/113112108): We need a check here that zero does not have federated
  # constituents.

  accumulate = value_impl.to_value(accumulate)
  merge = value_impl.to_value(merge)
  report = value_impl.to_value(report)
  for op in [accumulate, merge, report]:
    py_typecheck.check_type(op, value_base.Value)
    py_typecheck.check_type(op.type_signature, types.FunctionType)

  accumulate_type_expected = type_constructors.reduction_op(
      zero.type_signature, value.type_signature.member)
  merge_type_expected = type_constructors.reduction_op(zero.type_signature,
                                                       zero.type_signature)
  report_type_expected = types.FunctionType(zero.type_signature,
                                            report.type_signature.result)
  for op_name, op, type_expected in [('accumulate', accumulate,
                                      accumulate_type_expected),
                                     ('merge', merge, merge_type_expected),
                                     ('report', report, report_type_expected)]:
    if not type_expected.is_assignable_from(op.type_signature):
      raise TypeError('Expected parameter `{}` to be of type {}, '
                      'but received {} instead.'.format(op_name,
                                                        str(type_expected),
                                                        str(op.type_signature)))

  result_type = types.FederatedType(report.type_signature.result,
                                    placements.SERVER, True)
  intrinsic = value_impl.ValueImpl(
      computation_building_blocks.Intrinsic(
          intrinsic_defs.FEDERATED_AGGREGATE.uri,
          types.FunctionType([
              value.type_signature, zero.type_signature,
              accumulate_type_expected, merge_type_expected,
              report_type_expected
          ], result_type)))
  return intrinsic(value, zero, accumulate, merge, report)


def federated_average(value, weight=None):
  """Computes a `SERVER` average of `value` placed on `CLIENTS`.

  Args:
    value: The value to be averaged. Must be of a TFF federated type placed at
      `CLIENTS`. The value may be structured, e.g., its member constituents can
      be named tuples. The tensor types that the value is composed of must be
      floating-point or complex.
    weight: An optional weight, a TFF federated integer or floating-point tensor
      value, also placed at `CLIENTS`.

  Returns:
    A representation at the `SERVER` of an average of the member constituents
    of `value`, optionally weighted with `weight` if specified (otherwise, the
    member constituents contributed by all clients are equally weighted).

  Raises:
    TypeError: if `value` is not a federated TFF value placed at `CLIENTS`, or
      if `weight` is not a federated integer or a floating-point tensor with
      the matching placement.
  """
  # TODO(b/113112108): Possibly relax the constraints on numeric types, and
  # inject implicit casts where appropriate. For instance, we might want to
  # allow `tf.int32` values as the input, and automatically cast them to
  # `tf.float321 before invoking the average, thus producing a floating-point
  # result.

  # TODO(b/120439632): Possibly allow the weight to be either structured or
  # non-scalar, e.g., for the case of averaging a convolutional layer, when
  # we would want to use a different weight for every filter, and where it
  # might be cumbersome for users to have to manually slice and assemble a
  # variable.

  value = value_impl.to_value(value)
  type_utils.check_federated_value_placement(value, placements.CLIENTS,
                                             'value to be averaged')
  if not type_utils.is_average_compatible(value.type_signature):
    raise TypeError(
        'The value type {} is not compatible with the average operator.'.format(
            str(value.type_signature)))

  if weight is not None:
    weight = value_impl.to_value(weight)
    type_utils.check_federated_value_placement(weight, placements.CLIENTS,
                                               'weight to use in averaging')
    py_typecheck.check_type(weight.type_signature.member, types.TensorType)
    if weight.type_signature.member.shape.ndims != 0:
      raise TypeError('The weight type {} is not a federated scalar.'.format(
          str(weight.type_signature)))
    if not (weight.type_signature.member.dtype.is_integer or
            weight.type_signature.member.dtype.is_floating):
      raise TypeError('The weight type {} is not a federated integer or '
                      'floating-point tensor.'.format(
                          str(weight.type_signature)))

  result_type = types.FederatedType(value.type_signature.member,
                                    placements.SERVER, True)

  if weight is not None:
    intrinsic = value_impl.ValueImpl(
        computation_building_blocks.Intrinsic(
            intrinsic_defs.FEDERATED_WEIGHTED_AVERAGE.uri,
            types.FunctionType([value.type_signature, weight.type_signature],
                               result_type)))
    return intrinsic(value, weight)
  else:
    intrinsic = value_impl.ValueImpl(
        computation_building_blocks.Intrinsic(
            intrinsic_defs.FEDERATED_AVERAGE.uri,
            types.FunctionType(value.type_signature, result_type)))
    return intrinsic(value)


def federated_broadcast(value):
  """Broadcasts a federated value from the `SERVER` to the `CLIENTS`.

  Args:
    value: A value of a TFF federated type placed at the `SERVER`, all members
      of which are equal (the `all_equal` property of the federated type of
      `value` is True).

  Returns:
    A representation of the result of broadcasting: a value of a TFF federated
    type placed at the `CLIENTS`, all members of which are equal.

  Raises:
    TypeError: if the argument is not a federated TFF value placed at the
      `SERVER`.
  """
  value = value_impl.to_value(value)
  type_utils.check_federated_value_placement(value, placements.SERVER,
                                             'value to be broadcasted')

  if not value.type_signature.all_equal:
    raise TypeError('The broadcasted value should be equal at all locations.')

  # TODO(b/113112108): Replace this hand-crafted logic here and below with
  # a call to a helper function that handles it in a uniform manner after
  # implementing support for correctly typechecking federated template types
  # and instantiating template types on concrete arguments.
  result_type = types.FederatedType(value.type_signature.member,
                                    placements.CLIENTS, True)
  intrinsic = value_impl.ValueImpl(
      computation_building_blocks.Intrinsic(
          intrinsic_defs.FEDERATED_BROADCAST.uri,
          types.FunctionType(value.type_signature, result_type)))
  return intrinsic(value)


def federated_collect(value):
  """Materializes a federated value from `CLIENTS` as a `SERVER` sequence.

  Args:
    value: A value of a TFF federated type placed at the `CLIENTS`.

  Returns:
    A stream of the same type as the member constituents of `value` placed at
    the `SERVER`.

  Raises:
    TypeError: if the argument is not a federated TFF value placed at `CLIENTS`.
  """
  value = value_impl.to_value(value)
  type_utils.check_federated_value_placement(value, placements.CLIENTS,
                                             'value to be collected')

  result_type = types.FederatedType(
      types.SequenceType(value.type_signature.member), placements.SERVER, True)
  intrinsic = value_impl.ValueImpl(
      computation_building_blocks.Intrinsic(
          intrinsic_defs.FEDERATED_COLLECT.uri,
          types.FunctionType(value.type_signature, result_type)))
  return intrinsic(value)


def federated_map(value, mapping_fn):
  """Maps a federated value on CLIENTS pointwise using a given mapping function.

  Args:
    value: A value of a TFF federated type placed at the `CLIENTS`, or a value
      that can be implicitly converted into a TFF federated type, e.g., by
      zipping.
    mapping_fn: A mapping function to apply pointwise to member constituents of
      `value` on each of the participants in `CLIENTS`. The parameter of this
      function must be of the same type as the member constituents of `value`.

  Returns:
    A federated value on `CLIENTS` that represents the result of mapping.

  Raises:
    TypeError: if the arguments are not of the appropriates types.
  """

  # TODO(b/113112108): Possibly lift the restriction that the mapped value must
  # be placed at the clients after adding support for placement labels in the
  # federated types, and expanding the type specification of the intrinsic this
  # is based on to work with federated values of arbitrary placement.

  value = value_impl.to_value(value)
  if isinstance(value.type_signature, types.NamedTupleType):
    # TODO(b/120569877): Extend federated_zip to n-tuples
    if len(value.type_signature.elements) == 2:
      # We've been passed a value which the user expects to be zipped.
      value = federated_zip(value)
  type_utils.check_federated_value_placement(value, placements.CLIENTS,
                                             'value to be mapped')

  # TODO(b/113112108): Add support for polymorphic templates auto-instantiated
  # here based on the actual type of the argument.
  mapping_fn = value_impl.to_value(mapping_fn)

  py_typecheck.check_type(mapping_fn, value_base.Value)
  py_typecheck.check_type(mapping_fn.type_signature, types.FunctionType)
  if not mapping_fn.type_signature.parameter.is_assignable_from(
      value.type_signature.member):
    raise TypeError(
        'The mapping function expects a parameter of type {}, but member '
        'constituents of the mapped value are of incompatible type {}.'.format(
            str(mapping_fn.type_signature.parameter_type),
            str(value.type_signature.member)))

  # TODO(b/113112108): Replace this as noted above.
  result_type = types.FederatedType(mapping_fn.type_signature.result,
                                    placements.CLIENTS,
                                    value.type_signature.all_equal)
  intrinsic = value_impl.ValueImpl(
      computation_building_blocks.Intrinsic(
          intrinsic_defs.FEDERATED_MAP.uri,
          types.FunctionType(value.type_signature, result_type)))
  return intrinsic(value)


def federated_reduce(value, zero, op):
  """Reduces `value` from `CLIENTS` to `SERVER` using a reduction operator `op`.

  This method reduces a set of member constituents of a `value` of federated
  type `T@CLIENTS` for some `T`, using a given `zero` in the algebra (i.e., the
  result of reducing an empty set) of some type `U`, and a reduction operator
  `op` with type signature `(<U,T> -> U)` that incorporates a single `T`-typed
  member constituent of `value` into the `U`-typed result of partial reduction.
  In the special case of `T` equal to `U`, this corresponds to the classical
  notion of reduction of a set using a commutative associative binary operator.
  The generalized reduction (with `T` not equal to `U`) requires that repeated
  application of `op` to reduce a set of `T` always yields the same `U`-typed
  result, regardless of the order in which elements of `T` are processed in the
  course of the reduction.

  Args:
    value: A value of a TFF federated type placed at the `CLIENTS`.
    zero: The result of reducing a value with no constituents.
    op: An operator with type signature `(<U,T> -> U)`, where `T` is the type of
      the constituents of `value` and `U` is the type of `zero` to be used in
      performing the reduction.

  Returns:
    A representation on the `SERVER` of the result of reducing the set of all
    member constituents of `value` using the operator `op` into a single item.

  Raises:
    TypeError: if the arguments are not of the types specified above.
  """
  # TODO(b/113112108): Since in most cases, it can be assumed that CLIENTS is
  # a non-empty collective (or else, the computation fails), specifying zero
  # at this level of the API should probably be optional. TBD.

  value = value_impl.to_value(value)
  type_utils.check_federated_value_placement(value, placements.CLIENTS,
                                             'value to be reduced')

  zero = value_impl.to_value(zero)
  py_typecheck.check_type(zero, value_base.Value)

  # TODO(b/113112108): We need a check here that zero does not have federated
  # constituents.

  op = value_impl.to_value(op)
  py_typecheck.check_type(op, value_base.Value)
  py_typecheck.check_type(op.type_signature, types.FunctionType)
  op_type_expected = type_constructors.reduction_op(zero.type_signature,
                                                    value.type_signature.member)
  if not op_type_expected.is_assignable_from(op.type_signature):
    raise TypeError('Expected an operator of type {}, got {}.'.format(
        str(op_type_expected), str(op.type_signature)))

  # TODO(b/113112108): Replace this as noted above.
  result_type = types.FederatedType(zero.type_signature, placements.SERVER,
                                    True)
  intrinsic = value_impl.ValueImpl(
      computation_building_blocks.Intrinsic(
          intrinsic_defs.FEDERATED_REDUCE.uri,
          types.FunctionType(
              [value.type_signature, zero.type_signature, op_type_expected],
              result_type)))
  return intrinsic(value, zero, op)


def federated_sum(value):
  """Computes a sum at `SERVER` of a federated value placed on the `CLIENTS`.

  Args:
    value: A value of a TFF federated type placed at the `CLIENTS`.

  Returns:
    A representation of the sum of the member constituents of `value` placed
    on the `SERVER`.

  Raises:
    TypeError: if the argument is not a federated TFF value placed at `CLIENTS`.
  """
  value = value_impl.to_value(value)
  type_utils.check_federated_value_placement(value, placements.CLIENTS,
                                             'value to be summed')

  if not type_utils.is_sum_compatible(value.type_signature):
    raise TypeError(
        'The value type {} is not compatible with the sum operator.'.format(
            str(value.type_signature)))

  # TODO(b/113112108): Replace this as noted above.
  result_type = types.FederatedType(value.type_signature.member,
                                    placements.SERVER, True)
  intrinsic = value_impl.ValueImpl(
      computation_building_blocks.Intrinsic(
          intrinsic_defs.FEDERATED_SUM.uri,
          types.FunctionType(value.type_signature, result_type)))
  return intrinsic(value)


def federated_zip(value):
  """Converts a 2-tuple of federated values into a federated 2-tuple value.

  Args:
    value: A value of a TFF named tuple type with two elements, both of which
      are federated values placed at the `CLIENTS`.

  Returns:
    A federated value placed at the `CLIENTS` in which every member component
    at the given client is a two-element named tuple that consists of the pair
    of the corresponding member components of the elements of `value` residing
    at that client.

  Raises:
    TypeError: if the argument is not a named tuple of federated values placed
    at 'CLIENTS`.
  """
  # TODO(b/113112108): Extend this to accept named tuples of arbitrary length.

  # TODO(b/113112108): Extend this to accept *args.

  value = value_impl.to_value(value)
  py_typecheck.check_type(value, value_base.Value)
  py_typecheck.check_type(value.type_signature, types.NamedTupleType)
  num_elements = len(value.type_signature.elements)
  if num_elements != 2:
    raise TypeError(
        'The federated zip operator currently only supports zipping '
        'two-element tuples, but the tuple given as argument has {} '
        'elements.'.format(num_elements))
  for _, elem in value.type_signature.elements:
    py_typecheck.check_type(elem, types.FederatedType)
    if elem.placement is not placements.CLIENTS:
      raise TypeError(
          'The elements of the named tuple to zip must be placed at CLIENTS.')

  # TODO(b/113112108): Replace this as noted above.
  result_type = types.FederatedType(
      [e.member for _, e in value.type_signature.elements], placements.CLIENTS,
      all(e.all_equal for _, e in value.type_signature.elements))
  intrinsic = value_impl.ValueImpl(
      computation_building_blocks.Intrinsic(
          intrinsic_defs.FEDERATED_ZIP.uri,
          types.FunctionType(value.type_signature, result_type)))
  return intrinsic(value)
