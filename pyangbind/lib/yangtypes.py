"""
Copyright 2015, Rob Shakir (rjs@jive.com, rjs@rob.sh)

Modifications copyright 2016, Google Inc.

This project has been supported by:
          * Jive Communications, Inc.
          * BT plc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from __future__ import unicode_literals

import collections
import copy
import functools
import uuid
from decimal import Decimal

import regex
import six
import json
from bitarray import bitarray


LRU_CACHE_SIZE = 500000  # actual cache size is bounded and should be much smaller

# Words that could turn up in YANG definition files that are actually
# reserved names in Python, such as being builtin types. This list is
# not complete, but will probably continue to grow.
reserved_name = [
    "list",
    "str",
    "int",
    "global",
    "decimal",
    "float",
    "as",
    "if",
    "else",
    "elif",
    "map",
    "set",
    "class",
    "from",
    "import",
    "pass",
    "return",
    "is",
    "exec",
    "pop",
    "insert",
    "remove",
    "add",
    "delete",
    "local",
    "get",
    "default",
    "yang_name",
    "def",
    "print",
    "del",
    "break",
    "continue",
    "raise",
    "in",
    "assert",
    "while",
    "for",
    "try",
    "finally",
    "with",
    "except",
    "lambda",
    "or",
    "and",
    "not",
    "yield",
    "property",
    "min",
    "max",
]


def is_yang_list(arg):
    if isinstance(arg, list):
        return True
    elif hasattr(arg, "_pybind_generated_by"):
        pygen = getattr(arg, "_pybind_generated_by")
        if pygen in ["TypedListType", "YANGListType"]:
            return True
    return False


def is_yang_leaflist(arg):
    pygen = getattr(arg, "_pybind_generated_by", None)
    if pygen is None:
        return False
    elif pygen == "TypedListType":
        return True
    return False


def remove_path_attributes(p):
    new_path = []
    for i in p:
        if "[" in i:
            new_path.append(i.split("[")[0])
        else:
            new_path.append(i)
    return new_path


def safe_name(arg):
    """
    Make a leaf or container name safe for use in Python.
    """
    arg = arg.replace("-", "_")
    arg = arg.replace(".", "_")
    if arg in reserved_name:
        arg += "_"
    # store the unsafe->original version mapping
    # so that we can retrieve it when get() is called.
    return arg


@functools.lru_cache(LRU_CACHE_SIZE)
def RestrictedPrecisionDecimalType(precision=False):
    """
    Function to return a new type that is based on decimal.Decimal with
    an arbitrary restricted precision.
    """
    class RestrictedPrecisionDecimal(Decimal):
        """
        Class extending decimal.Decimal to restrict the precision that is
        stored, supporting the fraction-digits argument of the YANG decimal64
        type.
        """
        _precision = 10.0 ** (-1.0 * int(precision))
        _pybind_generated_by = "RestrictedPrecisionDecimal"

        def __new__(self, *args, **kwargs):
            """
            Overloads the decimal __new__ function in order to round the input
            value to the new value.
            """
            if self._precision is not None:
                if len(args):
                    try:
                        value = Decimal(args[0]).quantize(Decimal(str(self._precision)))
                    except TypeError:
                        raise ValueError("Invalid decimal number")
                else:
                    value = Decimal(0)
            elif len(args):
                value = Decimal(args[0])
            else:
                value = Decimal(0)
            obj = Decimal.__new__(self, value, **kwargs)
            return obj

    return RestrictedPrecisionDecimal


def RestrictedClassType(
        *, base_type=six.text_type, restriction_type=None, restriction_arg=None, restriction_dict=None, int_size=None):
    if restriction_dict is not None:
        restriction_dict = json.dumps(restriction_dict)
    if restriction_arg is not None:
        restriction_arg = json.dumps(restriction_arg)
    return RestrictedClassType__inner(
        base_type=base_type, restriction_type=restriction_type, restriction_arg=restriction_arg,
        restriction_dict=restriction_dict, int_size=int_size)


@functools.lru_cache(LRU_CACHE_SIZE)
def RestrictedClassType__inner(*, base_type, restriction_type, restriction_arg, restriction_dict, int_size):
    """
    Function to return a new type that restricts an arbitrary base_type with
    a specified restriction. The restriction_type specified determines the
    type of restriction placed on the class, and the restriction_arg gives
    any input data that this function needs.
    """
    if restriction_dict is not None:
        restriction_dict = json.loads(restriction_dict)
    if restriction_arg is not None:
        restriction_arg = json.loads(restriction_arg)

    # this gives deserialisers some hints as to how to encode/decode this value
    # it must be a list since a restricted class can encapsulate a restricted
    # class
    current_restricted_class_type = regex.sub("<(type|class) '(?P<class>.*)'>", "\g<class>", six.text_type(base_type))
    if hasattr(base_type, "_restricted_class_base"):
        restricted_class_hint = getattr(base_type, "_restricted_class_base")
        restricted_class_hint.append(current_restricted_class_type)
    else:
        restricted_class_hint = [current_restricted_class_type]

    class RestrictedClass(base_type):
        """
        A class that restricts the base_type class with a new function that the
        input value is validated against before being applied. The function is
        a static method which is assigned to _restricted_test.
        """
        _pybind_generated_by = "RestrictedClassType"

        _restricted_class_base = restricted_class_hint
        _restricted_int_size = int_size

        def __init__(self, *args, **kwargs):
            """
            Overloads the base_class __init__ method to check the input argument
            against the validation function - returns on instance of the base_type
            class, which can be manipulated as per a usual Python object.
            """
            try:
                self.__check(args[0])
            except IndexError:
                pass
            try:
                super(RestrictedClass, self).__init__(*args, **kwargs)
            except TypeError as e:
                super(RestrictedClass, self).__init__()

        def __new__(self, *args, **kwargs):
            self._restriction_dict = restriction_dict
            self._restriction_tests = []

            """
            Create a new class instance, and dynamically define the
            _restriction_test method so that it can be called by other functions.
            """

            range_regex = regex.compile("(?P<low>\-?[0-9\.]+|min)([ ]+)?\.\.([ ]+)?" + "(?P<high>(\-?[0-9\.]+|max))")
            range_single_value_regex = regex.compile("(?P<value>\-?[0-9\.]+)")

            def convert_regexp(pattern):

                # Some patterns include a $ character in them in some IANA modules, this
                # is not escaped. Do some logic to escape them, whilst leaving one at the
                # end of the string if it's there.
                trimmed = False
                if pattern[-1] == "$":
                    tmp_pattern = pattern[:-1]
                    trimmed = True
                else:
                    tmp_pattern = pattern
                tmp_pattern = tmp_pattern.replace("$", "\$")
                pattern = tmp_pattern
                if trimmed:
                    pattern += "$"

                if not pattern[0] == "^":
                    pattern = "^%s" % pattern
                if not pattern[len(pattern) - 1] == "$":
                    pattern = "%s$" % pattern

                return pattern

            def build_length_range_tuples(range, length=False, multiplier=1):
                if range_regex.match(range_spec):
                    low, high = range_regex.sub("\g<low>,\g<high>", range_spec).split(",")
                    if not length:
                        high = base_type(high) if not high == "max" else None
                        low = base_type(low) if not low == "min" else None
                    else:
                        high = int(high) * multiplier if not high == "max" else None
                        low = int(low) * multiplier if not low == "min" else None
                    return (low, high)
                elif range_single_value_regex.match(range_spec):
                    eqval = range_single_value_regex.sub("\g<value>", range_spec)
                    if not length:
                        eqval = base_type(eqval) if eqval not in ["max", "min"] else None
                    else:
                        eqval = int(eqval) * multiplier
                    return (eqval,)
                else:
                    raise ValueError("Invalid range or length argument specified")

            def in_range_check(low_high_tuples, length=False):

                def range_check(value):
                    if length and isinstance(value, bitarray):
                        value = value.length()
                    elif length:
                        value = len(value)
                    range_results = []
                    for check_tuple in low_high_tuples:
                        chk = True
                        if len(check_tuple) == 2:
                            if check_tuple[0] is not None and value < check_tuple[0]:
                                chk = False
                            if check_tuple[1] is not None and value > check_tuple[1]:
                                chk = False
                        elif len(check_tuple) == 1:
                            if value != float(check_tuple[0]):
                                chk = False
                        else:
                            raise AttributeError("Invalid check tuple length specified")
                        range_results.append(chk)
                    return True in range_results

                return range_check

            def match_pattern_check(regexp):

                def mp_check(value):
                    if not isinstance(value, six.string_types + (six.text_type,)):
                        return False
                    if regex.match(convert_regexp(regexp), value):
                        return True
                    return False

                return mp_check

            def in_dictionary_check(dictionary):
                return lambda i: six.text_type(i) in dictionary

            val = False
            try:
                val = args[0]
            except IndexError:
                pass
            if self._restriction_dict is None:
                if restriction_type is not None and restriction_arg is not None:
                    self._restriction_dict = {restriction_type: restriction_arg}
                else:
                    raise ValueError("must specify either a restriction dictionary or" + " a type and argument")

            for rtype, rarg in self._restriction_dict.items():
                if rtype == "pattern":
                    self._restriction_tests.append(match_pattern_check(rarg))
                elif rtype == "range":
                    ranges = []
                    for range_spec in rarg:
                        ranges.append(build_length_range_tuples(range_spec))
                    self._restriction_tests.append(in_range_check(ranges))
                    if val is not False:
                        try:
                            val = base_type(val)
                        except Exception:
                            raise ValueError("must specify a numeric type for a range " + "argument")
                elif rtype == "length":
                    # When the type is a binary then the length is specified in
                    # octets rather than bits, so we must specify the length to
                    # be multiplied by 8.
                    multiplier = 1
                    if base_type == bitarray:
                        multiplier = 8
                    lengths = []
                    for range_spec in rarg:
                        lengths.append(build_length_range_tuples(range_spec, length=True, multiplier=multiplier))
                    self._restriction_tests.append(in_range_check(lengths, length=True))
                elif rtype == "dict_key":
                    new_rarg = copy.deepcopy(rarg)
                    for k in rarg:
                        if k.startswith("@"):
                            new_rarg.pop(k, None)
                    # populate enum values
                    used_values = []
                    for k in new_rarg:
                        if "value" in new_rarg[k]:
                            used_values.append(int(new_rarg[k]["value"]))
                    c = 0
                    for k in new_rarg:
                        while c in used_values:
                            c += 1
                        if "value" not in new_rarg[k]:
                            new_rarg[k]["value"] = c
                        c += 1
                    self._restriction_tests.append(in_dictionary_check(new_rarg))
                    self._enumeration_dict = new_rarg
                else:
                    raise ValueError("unsupported restriction type")

            if val is not False:
                for test in self._restriction_tests:
                    passed = False
                    if test(val) is not False:
                        passed = True
                        break
                if not passed:
                    raise ValueError("%s does not match a restricted type" % val)

            try:
                obj = base_type.__new__(self, *args, **kwargs)
            except TypeError:
                obj = base_type.__new__(self)
            return obj

        def __check(self, v):
            """
            Run the _restriction_test static method against the argument v,
            returning an error if the value does not validate.
            """
            v = base_type(v)
            for chkfn in self._restriction_tests:
                if not chkfn(v):
                    raise ValueError("did not match restricted type")
            return True

        def getValue(self, *args, **kwargs):
            """
            For types where there is a dict_key restriction (such as YANG
            enumeration), return the value of the dictionary key.
            """
            if "dict_key" in self._restriction_dict:
                value = kwargs.pop("mapped", False)
                if value:
                    return self._enumeration_dict[self.__str__()]["value"]
            return self

    return RestrictedClass


def TypedListType(allowed_type=six.text_type):
    if isinstance(allowed_type, list):
        allowed_type = tuple(allowed_type)
    return TypedListType__inner(allowed_type)


@functools.lru_cache(LRU_CACHE_SIZE)
def TypedListType__inner(allowed_type):
    """
    Return a type that consists of a list object where only
    certain types (specified by allowed_type kwarg to the function)
    can be added to the list.
    """
    if not isinstance(allowed_type, tuple):
        allowed_type = (allowed_type,)

    class TypedList(collections.abc.MutableSequence):
        _pybind_generated_by = "TypedListType"
        _list = list()

        def __init__(self, *args, **kwargs):
            self._unique = kwargs.pop("unique", False)
            self._allowed_type = allowed_type
            self._list = list()
            if len(args):
                if isinstance(args[0], list):
                    tmp = []
                    for i in args[0]:
                        if not self._unique or i not in tmp:
                            tmp.append(self.check(i))
                    self._list.extend(tmp)
                else:
                    tmp = self.check(args[0])
                    self._list.append(tmp)

        def check(self, v):
            # Short circuit uniqueness check
            if self._unique and v in self._list:
                raise ValueError("Values in this list must be unique.")

            passed = False
            count = 0
            path_keystring = "[.='%s']" % v

            def dyn_wrapper(base):
                return YANGDynClass(
                  v,
                  base=base,
                  yang_name=self._yang_name,
                  parent=self,
                  is_leaf=True,
                  path_helper=self._path_helper,
                  extmethods=self._extmethods,
                  register_paths=self._register_paths,
                  yang_type=self._yang_type,
                  namespace=self._namespace,
                  defining_module=self._defining_module,
                  register_path=(self._parent._path() + [self._yang_name + path_keystring]),
                  register_path_ietf=(self._parent._path(True) + [yname_ns_func(self) + path_keystring]),
                  is_config=self._is_config
                )
            for i in self._allowed_type:
                if isinstance(v, i):
                    tmp = dyn_wrapper(i)
                    passed = True
                    break
                try:
                    if hasattr(i, "_pybind_generated_by"):
                        attr = getattr(i, "_pybind_generated_by")
                        if attr in ["RestrictedClassType", "ReferencePathType", "RestrictedPrecisionDecimal"]:
                            tmp = dyn_wrapper(i)
                            passed = True
                            break
                    elif i == six.text_type and isinstance(v, six.string_types + (six.text_type,)):
                        tmp = dyn_wrapper(i)  # tmp = six.text_type(v)
                        passed = True
                        break
                    elif i not in six.string_types + (six.text_type,):
                        # for anything other than string we try
                        # and cast. Using things for string or
                        # unicode gives us strange results because we get
                        # class name represetnations
                        tmp = i(v)
                        passed = True
                        break
                except Exception as e:
                    # we catch all exceptions because we duck-type as
                    # much as possible and some types - e.g., decimal do
                    # not use builtins.
                    pass
                count += 1
            if not passed:
                raise ValueError("Cannot add %s to TypedList (accepts only %s)" % (v, self._allowed_type))
            else:
                return tmp

        def __len__(self):
            return len(self._list)

        def __getitem__(self, i):
            return self._list[i]

        def __delitem__(self, i):
            del self._list[i]

        def __setitem__(self, i, v):
            self.insert(i, v)

        def insert(self, i, v):
            val = self.check(v)
            self._list.insert(i, val)

        def append(self, v):
            if not self._unique or v not in self._list:
                val = self.check(v)
                self._list.append(val)

        def __str__(self):
            return str(self._list)

        def __iter__(self):
            return iter(self._list)

        def __eq__(self, other):
            if self._list == other:
                return True
            return False

        def get(self, filter=False):
            return self._list

    return TypedList


def yname_ns_func(element):
    if not element._namespace == (getattr(element._parent, '_namespace', None) or getattr(element._parent, '_yang_namespace')):
        # if the namespace is different, then precede with the module
        # name as per spec.
        return "%s:%s" % (element._defining_module, element._yang_name)
    else:
        return element._yang_name


def YANGListType(
        keyname, listclass, *, is_container=False, yang_name=False, yang_keys=False, user_ordered=False,
        # these params are passed twice by the code generator - here and in the YangDynClass Wrapper
        path_helper=None, extensions=None,
        # these params are passed by the code generator, but not used here
        parent=False):
    return YANGListType__inner(keyname, listclass, is_container=is_container, yang_name=yang_name, yang_keys=yang_keys,
                               user_ordered=user_ordered)


@functools.lru_cache(LRU_CACHE_SIZE)
def YANGListType__inner(
        keyname, listclass, *, is_container, yang_name, yang_keys, user_ordered):
    """
    Return a type representing a YANG list, with a contained class.
    An ordered dict is used to store the list, and the
    returned object behaves akin to a dictionary.

    Additional checks are performed to ensure that the keys of the
    list are valid before adding the value.

    .add(key) - initialises a new member of the list
    .delete(key) - removes it.

    Where a list exists that does not have a key - which can be the
    case for 'config false' lists - a uuid is generated and used
    as the key for the list.
    """

    class YANGList(object):
        __slots__ = ("_members", "_keyval", "_contained_class", "_path_helper", "_yang_keys", "_ordered")
        _pybind_generated_by = "YANGListType"

        def __init__(self):
            self._ordered = True if user_ordered else False
            self._members = collections.OrderedDict()

            self._members._user_ordered = True if user_ordered else False

            self._keyval = keyname
            if not type(listclass) == type(int):
                raise ValueError("contained class of a YANGList must be a class")
            self._contained_class = listclass
            # self._path_helper = path_helper   # no need - shadowed by YangDynClass
            self._yang_keys = yang_keys

        def __str__(self):
            return str(self._members)

        def __repr__(self):
            return repr(self._members)

        def __check__(self, v):
            if self._contained_class is None:
                return False
            try:
                tmp = YANGDynClass(
                    base=self._contained_class,
                    parent=self,
                    yang_name=yang_name,
                    is_container=is_container,
                    path_helper=False,
                    defining_module=None,
                    namespace=None
                )
                valid = False
                if not tmp.__slots__ == v.__slots__:
                    valid = True
                elif self._contained_class.__slots__ == v.__slots__:
                    valid = True
                if valid is False:
                    return valid
            except Exception:
                return False
            return True

        def iteritems(self):
            return six.iteritems(self._members)

        def itervalues(self):
            return six.itervalues(self._members)

        def _key_to_native_key_type(self, k):
            if self._keyval is False:
                raise AttributeError("List does not have a key")
            elif " " in self._keyval:
                raise AttributeError("Multiple key, string type should be used")
            else:
                member = self._members[k]
                getfn = getattr(member, "_get_%s" % self._keyval)
                return getfn()

        def __iter__(self):
            return iter(self._members)

        def __contains__(self, k):
            if k in self._members:
                return True
            return False

        def __getitem__(self, k):
            return self._members[k]

        def __setitem__(self, k, v):
            self.__set(_k=k, _v=v)

        def __set(self, *args, **kwargs):
            k = kwargs.pop("_k", None)
            v = kwargs.pop("_v", None)
            named_set = kwargs.pop("_named_set", False)

            if k is None and self._keyval and not named_set:
                k = args[0]
            elif k is None:
                # this is a list that does not have a key specified, and hence
                # we generate a uuid that is used as the key, the method then
                # returns the uuid for the upstream process to use
                k = six.text_type(uuid.uuid1())

            update = False
            if v is not None:
                if not self.__check__(v):
                    raise ValueError("value must be set to an instance of %s" % (self._contained_class))
                else:
                    update = True

            if k in self._members:
                update = True

            if self._keyval:
                try:
                    tmp = YANGDynClass(
                        base=self._contained_class,
                        parent=self,
                        yang_name=yang_name,
                        is_container="container",
                        defining_module=self._defining_module,
                        namespace=self._namespace,
                        path_helper=False,
                    )
                    keydict = None

                    if " " in self._keyval and not named_set:
                        keys = self._keyval.split(" ")
                        keyparts = k.split(" ")
                        keydict = {}
                        for kp, kv in zip(keys, keyparts):
                            keydict[kp] = kv

                        if not len(keyparts) == len(keys):
                            raise KeyError(
                                "YANGList key must contain all key elements (%s)" % (self._keyval.split(" "))
                            )
                    elif named_set:
                        k = kwargs.pop("_python_key", None)
                        keydict = copy.copy(kwargs)
                    else:
                        if k == "":
                            raise KeyError("Cannot set a null key for a list entry!")
                        keydict = {self._keyval: k}
                        kv_obj = getattr(tmp, self._keyval)
                        path_keystring = "[%s='%s']" % (kv_obj.yang_name(), k)

                    if keydict is not None:
                        keys = self._keyval.split(" ")
                        path_keystring = "["
                        for kv in keys:
                            kv_obj = getattr(tmp, kv)
                            path_keystring += "%s='%s' " % (kv_obj.yang_name(), keydict[kv])
                        path_keystring = path_keystring[:-1]
                        path_keystring += "]"

                    if not update:
                        tmp = YANGDynClass(
                            base=self._contained_class,
                            parent=self,
                            yang_name=yang_name,
                            is_container="container",
                            defining_module=self._defining_module,
                            namespace=self._namespace,
                            path_helper=self._path_helper,
                            register_path=(self._parent._path() + [self._yang_name + path_keystring]),
                            register_path_ietf=(self._parent._path(True) + [yname_ns_func(self) + path_keystring]),
                            extmethods=self._parent._extmethods,
                            extensions=self._extensionsd,
                        )
                    else:
                        # hand the value to the init, rather than simply creating an empty
                        # object.
                        tmp = YANGDynClass(
                            v,
                            base=self._contained_class,
                            parent=self,
                            yang_name=yang_name,
                            is_container="container",
                            defining_module=self._defining_module,
                            namespace=self._namespace,
                            path_helper=self._path_helper,
                            register_path=(self._parent._path() + [self._yang_name + path_keystring]),
                            register_path_ietf=(self._parent._path(True) + [yname_ns_func(self) + path_keystring]),
                            extmethods=self._parent._extmethods,
                            load=True,
                            extensions=self._extensionsd,
                        )

                    if keydict is not None:
                        for kn in keydict:
                            key = getattr(tmp, "_set_%s" % safe_name(kn))
                            key(keydict[kn], load=True)

                    if hasattr(k, "_referenced_object") and k._referenced_object is not None:
                        k = k._referenced_object

                    self._members[k] = tmp

                except ValueError as m:
                    raise KeyError("key value must be valid, %s" % m)
            else:
                self._members[k] = YANGDynClass(
                    base=self._contained_class,
                    parent=self,
                    yang_name=yang_name,
                    is_container=is_container,
                    defining_module=self._defining_module,
                    namespace=self._namespace,
                    path_helper=self._path_helper,
                    extmethods=self._parent._extmethods,
                    extensions=self._extensionsd,
                )
                return k

        def __delitem__(self, k):
            del self._members[k]

        def __len__(self):
            return len(self._members)

        def keys(self):
            return self._members.keys()

        def items(self):
            return self._members.items()

        def values(self):
            return self._members.values()

        def _generate_key(self, *args, **kwargs):
            keyargs = None
            if len(args):
                k = args[0]
            elif len(kwargs):
                keyargs = {}
                k = ""
                for kn in self._keyval.split(" "):
                    try:
                        keyargs[kn] = kwargs[kn]
                    except KeyError as m:
                        raise AttributeError(
                            "Keyword list add function must have all " + "keys specified - cannot find %s" % m
                        )
                    k += "%s " % kwargs[kn]
                k = k[:-1]
            else:
                k = None
            return (k, keyargs)

        def _extract_key(self, obj):
            kp = self._keyval.split(" ")
            if len(kp) > 1:
                ks = ""
                for k in kp:
                    kv = getattr(obj, "_get_%s" % safe_name(k), None)
                    if kv is None:
                        raise KeyError("Invalid key attribute specified for object")
                    ks += "%s " % six.text_type(kv())
                return ks.rstrip(" ")
            else:
                kv = getattr(obj, "_get_%s" % safe_name(self._keyval), None)
                if kv is None:
                    raise KeyError("Invalid key attribute specified for object: %s" % self._keyval)
                return kv()

        def append(self, obj):
            self.__set(_k=self._extract_key(obj), _v=obj)

        def _new_item(self):
            return self._contained_class()

        def add(self, *args, **kwargs):
            if len(args) and len(kwargs):
                raise AttributeError("Cannot add an entry to a list based on both " + " keywords and string args")

            value = kwargs.pop("_v", None)

            (k, keyargs) = self._generate_key(*args, **kwargs)

            if k in self._members:
                raise KeyError("%s is already defined as a list entry" % k)
            if self._keyval and keyargs is None:
                if k is None:
                    raise KeyError("a list with a key value must have a key specified")
                self.__set(_k=k)
                return self._members[k]
            elif self._keyval and keyargs is not None:
                keyargs["_python_key"] = k
                keyargs["_named_set"] = True
                if value is not None:
                    keyargs["_v"] = value
                self.__set(**keyargs)
                return self._members[k]
            else:
                k = self.__set()
                return k

        def delete(self, *args, **kwargs):
            (k, _) = self._generate_key(*args, **kwargs)

            if self._path_helper:
                current_item = self._members[k]
                if " " in self._keyval:
                    keyparts = self._keyval.split(" ")
                    keyargs = k.split(" ")
                    key_string = "["
                    for key, val in zip(keyparts, keyargs):
                        kv_o = getattr(current_item, key)
                        key_string += "%s=%s " % (kv_o.yang_name(), val)
                    key_string = key_string.rstrip(" ")
                    key_string += "]"
                else:
                    kv_o = getattr(current_item, self._keyval)
                    key_string = "[@%s=%s]" % (kv_o.yang_name(), k)

                obj_path = self._parent._path() + [self._yang_name + key_string]

            try:
                del self._members[k]
                if self._path_helper:
                    self._path_helper.unregister(obj_path)
            except KeyError as m:
                raise KeyError("key %s was not in list (%s)" % (k, m))

        def _item(self, *args, **kwargs):
            keystr = ""
            if " " in self._keyval:
                keyparts = self._keyval.split(" ")
            else:
                keyparts = [self._keyval]
            for kn in keyparts:
                try:
                    keystr += "%s " % kwargs[kn]
                except KeyError:
                    raise KeyError("Must specify all keys to retrieve a list entry")
            keystr = keystr[:-1]

            return self._members[keystr]

        def get(self, filter=False):
            d = collections.OrderedDict()
            d._user_ordered = self._members._user_ordered
            for i in self._members:
                if hasattr(self._members[i], "get"):
                    d[i] = self._members[i].get(filter=filter)
                else:
                    d[i] = self._members[i]
            return d

    return YANGList


class YANGBool(int):
    """
    A custom boolean class for using in YANG. Since bool has specific
    logic in python, it is not possible to extend the existing bool
    objects.

    This bool also accepts input matching strings to handle the
    forms that might be used in YANG modules.
    """

    def __new__(self, *args, **kwargs):
        false_args = ["false", "False", False, 0, "0"]
        true_args = ["true", "True", True, 1, "1"]
        if len(args):
            if not args[0] in false_args + true_args:
                raise ValueError("%s is an invalid value for a YANGBool" % args[0])
            value = 0 if args[0] in false_args else 1
        else:
            value = 0
        return int.__new__(self, bool(value))

    def __repr__(self):
        return str([False, True][self])

    def __str__(self):
        return str(self.__repr__())


def YANGDynClass(*args, base, yang_name, parent, path_helper, namespace, defining_module,
                 is_container=False, yang_type=None, default=False, choice=False, is_leaf=False, register_path=None,
                 register_path_ietf=None, extensions=None, extmethods=False, is_keyval=False, register_paths=True,
                 load=None, is_config=True, presence=None, **kwargs):
    # TODO: fix broken 'extmethods' support. was broken in order to allow lru-caching of dynamic classes creation
    assert extmethods is False

    if isinstance(base, list):
        # this is a union, we must infer type
        if not len(args):
            # there is no argument to infer the type from
            # so use the first type (default)
            base = base[0]
        else:
            type_test = False
            for candidate_type in base:
                try:
                    type_test = candidate_type(args[0])  # does the slipper fit?
                    # hop, skip and jump with the last candidate
                    base = candidate_type
                    break
                except Exception:
                    pass  # don't worry, move on, plenty more fish (types) in the sea...
            if type_test is False:
                # we're left alone at midnight -- no types fit the arguments
                raise TypeError("did not find a valid type using the argument as a hint")

    clazz = YANGDynClass__inner(base_type=base, is_container=is_container, yang_type=yang_type)
    return clazz(*args, default=default, yang_name=yang_name, parent=parent, choice=choice, is_leaf=is_leaf,
                 path_helper=path_helper, register_path=register_path, register_path_ietf=register_path_ietf,
                 extensions=extensions, extmethods=extmethods, is_keyval=is_keyval, register_paths=register_paths,
                 namespace=namespace, defining_module=defining_module, load=load, is_config=is_config,
                 presence=presence, extra_kwargs=kwargs)


@functools.lru_cache(LRU_CACHE_SIZE)
def YANGDynClass__inner(base_type, is_container, yang_type):
    """
    Wrap an type - specified in the base_type arugment - with
    a set of custom attributes that YANG specifies (or are required
    for serialisation of that object). Particularly:

      - base_type:  the original type - for example, string, int.
      - default:    the YANG specified default value of the type.
      - yang_name:  the YANG name of the type (as opposed to a 'safe'
                    Python version).
      - parent:     the class which this type is a member of in the
                    YANG-specified tree.
      - choice:     The choice branch that this type is a member of.
      - is_{container,leaf}: whether this element is a container or
                             a leaf.
      - path_helper: pyangbind helper class to allow XPATH lookups.
      - supplied_register_path: an override for the path that this
                                object should register to. This is
                                used when an element is a member of
                                a list to add the key attributes to
                                the path.
      - extensions:  The list of extensions that should be stored
                     with the type.
      - is_config:   Whether this is a configuration (editable)
                     node.
      - presence:    Whether the YANG container that is being
                     represented has the presence keyword
    """

    clsslots = [
        "_default",
        "_mchanged",
        "_yang_name",
        "_choice",
        "_parent",
        "_supplied_register_path",
        "_supplied_register_path_ietf",
        "_path_helper",
        "_base_type",
        "_is_leaf",
        "_is_container",
        "_extensionsd",
        "_extmethods",
        "_is_keyval",
        "_register_paths",
        "_namespace",
        "_yang_type",
        "_defining_module",
        "_metadata",
        "_is_config",
        "_cpresent",
        "_presence",
    ]

    # TODO: fix broken 'extmethods' support. was broken in order to allow lru-caching of dynamic classes creation
    # if extmethods:
    #     rpath = None
    #     if supplied_register_path is not None:
    #         rpath = supplied_register_path
    #     if parent_instance is not None:
    #         rpath = parent_instance._path() + [yang_name]
    #     else:
    #         rpath = []
    #     chk_path = "/" + "/".join(remove_path_attributes(rpath))
    #     if chk_path in extmethods:
    #         for method in [i for i in dir(extmethods[chk_path]) if not i.startswith("_")]:
    #             clsslots.append("_" + method)

    class YANGBaseClass(base_type):
        # we only create slots for things that are restricted
        # in adding attributes to them - this means containing
        # data nodes. This means that we can allow
        # leaf._someattr to be used by consuming code - it
        # also fixes an issue whereby we could set __slots__
        # and try and inherit a variable-length inbuilt such
        # as long, which is not allowed.
        if yang_type in ["container", "list"] or is_container == "container":
            __slots__ = tuple(clsslots)

        _pybind_base_class = regex.sub("<(type|class) '(?P<class>.*)'>", "\g<class>", str(base_type))

        def __new__(self, *args, yang_name, parent, path_helper, namespace, defining_module, default, choice, is_leaf,
                    register_path, register_path_ietf, extensions, extmethods, is_keyval, register_paths, load,
                    is_config, presence, extra_kwargs):
            try:
                obj = base_type.__new__(self, *args, **extra_kwargs)
            except TypeError:
                obj = base_type.__new__(self)
            return obj

        def __init__(self, *args, yang_name, parent, path_helper, namespace, defining_module, default, choice, is_leaf,
                     register_path, register_path_ietf, extensions, extmethods, is_keyval, register_paths, load,
                     is_config, presence, extra_kwargs):
            self._default = False
            self._mchanged = False
            self._yang_name = yang_name
            self._parent = parent
            self._choice = choice
            self._path_helper = path_helper
            self._supplied_register_path = register_path
            self._supplied_register_path_ietf = register_path_ietf
            self._base_type = base_type
            self._is_leaf = is_leaf
            self._is_container = is_container
            self._is_config = is_config
            self._extensionsd = extensions
            self._extmethods = extmethods
            self._is_keyval = is_keyval
            self._register_paths = register_paths
            self._namespace = namespace
            self._yang_type = yang_type
            self._defining_module = defining_module
            self._metadata = {}
            self._presence = presence
            self._cpresent = False

            # TODO: fix broken 'extmethods' support. was broken in order to allow lrucaching of dynamic classes creation
            # if self._extmethods:
            #     chk_path = "/" + "/".join(remove_path_attributes(self._register_path()))
            #     if chk_path in self._extmethods:
            #         for method in [i for i in dir(self._extmethods[chk_path]) if not i.startswith("_")]:
            #             # Don't allow methods to be overwritten
            #             if hasattr(self, "_" + method):
            #                 continue
            #             member = getattr(self._extmethods[chk_path], method)
            #             if hasattr(member, "__call__"):
            #                 if not hasattr(self, "_method"):
            #                     setattr(self, "_" + method, self.__generate_extmethod(member))

            if default:
                self._default = default
            if len(args):
                self._set()

            # lists themselves do not register, only elements within them
            # are actually created in the tree.
            if not self._is_container == "list" and not is_yang_leaflist(self._parent):
                if self._path_helper:
                    if self._supplied_register_path is None:
                        if self._register_paths:
                            self._path_helper.register(self._register_path(), self)
                    else:
                        if self._register_paths:
                            self._path_helper.register(self._supplied_register_path, self)

            if self._is_container == "list" or self._is_container == "container":
                extra_kwargs["path_helper"] = self._path_helper
                if load is not None:
                    extra_kwargs["load"] = load

            try:
                super(YANGBaseClass, self).__init__(*args, **extra_kwargs)
            except TypeError:
                super(YANGBaseClass, self).__init__()

        def _changed(self):
            return self._mchanged

        def _extensions(self):
            return self._extensionsd

        def _path(self, ietf=False):
            return self._register_path(ietf)

        def _yang_path(self):
            return "/" + "/".join(self._register_path())

        def __str__(self):
            return super(YANGBaseClass, self).__str__()

        def __repr__(self):
            return super(YANGBaseClass, self).__repr__()

        def _set(self, choice=False):
            if choice:
                nested_choices, nested_cases_in_use = choice
                for nesting_level in reversed(range(1, 1 + len(nested_choices))):
                    used_cases_for_nesting_level = nested_cases_in_use[:nesting_level]
                    for cases, fields in self.__choices__[nested_choices[:nesting_level]].items():
                        if cases == used_cases_for_nesting_level:
                            continue

                        for elem in fields:
                            method = "_unset_%s" % elem
                            if not hasattr(self, method):
                                raise AttributeError("unmapped choice!")
                            x = getattr(self, method)
                            x()

            self._mchanged = True

            if self._presence:
                self._cpresent = True

            if self._parent not in [False, None]:  # and hasattr(self._parent, "_set"):    # parent MUST have a '_set'
                self._parent._set(choice=self._choice)

        def _add_metadata(self, k, v):
            self._metadata[k] = v

        def yang_name(self):
            return self._yang_name

        def default(self):
            return self._default

        # we need to overload the set methods
        def __setitem__(self, *args, **kwargs):
            self._set()
            super(YANGBaseClass, self).__setitem__(*args, **kwargs)

        def append(self, *args, **kwargs):
            if not hasattr(super(YANGBaseClass, self), "append"):
                raise AttributeError("%s object has no attribute append" % base_type)
            self._set()
            super(YANGBaseClass, self).append(*args, **kwargs)

        def pop(self, *args, **kwargs):
            if not hasattr(super(YANGBaseClass, self), "pop"):
                raise AttributeError("%s object has no attribute pop" % base_type)
            self._set()
            item = super(YANGBaseClass, self).pop(*args, **kwargs)
            return item

        def remove(self, *args, **kwargs):
            if not hasattr(super(YANGBaseClass, self), "remove"):
                raise AttributeError("%s object has no attribute remove" % base_type)
            self._set()
            if self._path_helper:
                elem_index = super(YANGBaseClass, self).index(*args, **kwargs)
                super(YANGBaseClass, self).__getitem__(elem_index)
            super(YANGBaseClass, self).remove(*args, **kwargs)

        def extend(self, *args, **kwargs):
            if not hasattr(super(YANGBaseClass, self), "extend"):
                raise AttributeError("%s object has no attribute extend" % base_type)
            self._set()
            super(YANGBaseClass, self).extend(*args, **kwargs)

        def insert(self, *args, **kwargs):
            if not hasattr(super(YANGBaseClass, self), "insert"):
                raise AttributeError("%s object has no attribute insert" % base_type)
            self._set()
            super(YANGBaseClass, self).insert(*args, **kwargs)

        def _register_path(self, ietf=False):
            if not ietf and self._supplied_register_path is not None:
                return self._supplied_register_path
            if ietf and self._supplied_register_path_ietf is not None:
                return self._supplied_register_path_ietf
            if self._parent is not None:
                return self._parent._path(ietf) + \
                       [(self._defining_module + ':'
                         if ietf and self._defining_module != getattr(self._parent,'_defining_module', None) else
                         '') + self._yang_name]
            else:
                return []

        def __generate_extmethod(self, methodfn):

            def extmethodfn(*args, **kwargs):
                kwargs["caller"] = self._register_path()
                kwargs["path_helper"] = self._path_helper
                return methodfn(*args, **kwargs)

            return extmethodfn

        def _set_present(self, present=True):
            if not self._is_container == "container":
                raise AttributeError("Cannot set presence on a non-container")
            self._cpresent = present
            if present is True:
                self._set()

        def _present(self):
            if not self._is_container == "container":
                return None

            if self._presence is False:
                return None

            return self._cpresent

    return YANGBaseClass


def ReferenceType(*, referenced_path, path_helper, caller, require_instance):
    # 'caller' is based on current tree path and therefore very bad of lru-cache, on the other hand - it is very
    # easily computed based on the parent path and the current segment yang name so we can just ignore it
    # 'path_helper' is passed twice by the code generator - here and in the YangDynClass Wrapper
    return ReferenceType__inner(referenced_path=referenced_path, require_instance=require_instance)


@functools.lru_cache(LRU_CACHE_SIZE)
def ReferenceType__inner(*, referenced_path, require_instance):
    """
    A type which based on a path provided acts as a leafref.
    The caller argument is used to allow the path that is provided
    to be a relative (rather than absolute) path. The require_instance
    argument specifies whether errors should be thrown in the case
    that the referenced instance does not exist.
    """

    class ReferencePathType(object):

        __slots__ = (
            "_referenced_path",
            "_path_helper",
            "_caller",
            "_referenced_object",
            "_ptr",
            "_require_instance",
            "_type",
            "_utype",
        )

        _pybind_generated_by = "ReferencePathType"

        def __init__(self, *args, **kwargs):
            self._referenced_path = referenced_path
            # self._path_helper = path_helper
            self._referenced_object = False
            self._caller = self._parent._path() + [self._yang_name]
            self._ptr = False
            self._require_instance = require_instance
            self._type = "unicode"
            self._utype = six.text_type

            if len(args):
                value = args[0]
                if hasattr(self, "_set"):
                    self._set()
            else:
                value = None

            if self._path_helper and value is not None:
                path_chk = self._path_helper.get(self._referenced_path, caller=self._caller)

                # if the lookup returns only one leaf, then this means that we have
                # something that could potentially be a pointer. However, this is not
                # sufficient to tell whether it is (it could be a single list entry)
                # - thus perform two additional checks. 1) check whether this is the
                # key value of a list (if it is then this is something that can be
                # externally referenced) and 2) check that this is not
                # a list itself (including a leaf-list)
                if len(path_chk) == 1 and not path_chk[0]._is_keyval and not is_yang_list(path_chk[0]):
                    # we are not checking whether this leaf exists, but rather
                    # this is a pointer to some other value.
                    path_parts = self._referenced_path.split("/")
                    leaf_name = path_parts[-1]
                    if ":" in leaf_name:
                        # normalise the namespace
                        leaf_name = leaf_name.split(":")[1]
                    set_method = getattr(path_chk[0]._parent, "_set_%s" % safe_name(leaf_name))
                    get_method = getattr(path_chk[0]._parent, "_get_%s" % safe_name(leaf_name))

                    if value is not None:
                        set_method(value)
                    self._type = regex.sub("<(type|class) '(?P<class>.*)'>", "\g<class>", str(get_method()._base_type))

                    self._utype = get_method()._base_type
                    self._ptr = True
                elif self._require_instance:
                    if value is None:
                        self._referenced_object = None
                    else:
                        found = False
                        path_chk = self._path_helper.get(self._referenced_path, caller=self._caller)

                        if len(path_chk) == 1 and is_yang_leaflist(path_chk[0]):
                            index = 0
                            for i in path_chk[0]:
                                if six.text_type(i) == six.text_type(value):
                                    found = True
                                    self._referenced_object = path_chk[0][index]
                                    break
                                index += 1
                        else:
                            found = False
                            for i in path_chk:
                                if six.text_type(i) == six.text_type(value):
                                    found = True
                                    self._referenced_object = i

                        if not found:
                            raise ValueError(
                                "no such key (%s) existed in path (%s -> %s)"
                                % (value, self._referenced_path, path_chk)
                            )
                else:
                    # require instance is not set, so act like the referenced type
                    self._referenced_object = value
            elif value is not None:
                # No path helper and a value is set, just act like the referenced type
                self._referenced_object = value

        def _get_ptr(self):
            if self._ptr:
                ptr = self._path_helper.get(self._referenced_path, caller=self._caller)
                if len(ptr) == 1:
                    return ptr[0]
            raise ValueError("Invalid pointer specified")

        def __repr__(self):
            if not self._ptr:
                return repr(self._referenced_object)
            return repr(self._get_ptr())

        def _get(self):
            if not self._ptr:
                return self._referenced_object
            return self._get_ptr()

        def __str__(self):
            if not self._ptr:
                return str(self._referenced_object)
            return str(self._get_ptr())

        def __eq__(self, other):
            return self is other or \
                   (isinstance(other, ReferencePathType) and
                    # 'getattr' hack as '__eq__' may be called when self/other are not fully constructed
                    # (super.__init__() not called yet) which is very difficult to resolve.
                    getattr(self, '_referenced_path', 1) == getattr(other, '_referenced_path', 2))

        def __hash__(self):
            return hash(self._referenced_path)

    return ReferencePathType
