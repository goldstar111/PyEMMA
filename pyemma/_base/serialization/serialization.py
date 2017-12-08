# This file is part of PyEMMA.
#
# Copyright (c) 2016, 2014 Computational Molecular Biology Group, Freie Universitaet Berlin (GER)
#
# PyEMMA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging

from pyemma._base.loggable import Loggable
from pyemma._base.serialization.jsonpickler_handlers import register_all_handlers as _reg_all_handlers
from pyemma._base.serialization.util import class_rename_registry
import jsonpickle
from jsonpickle.util import importable_name as _importable_name

from pyemma.util.types import is_int

logger = logging.getLogger(__name__)
_debug = False

if _debug:
    logger.level = logging.DEBUG

# indicate whether serialization handlers have already been registered
_handlers_registered = False


class DeveloperError(Exception):
    """ the devs have done something wrong. """


class OldVersionUnsupported(NotImplementedError):
    """ can not load recent models with old software versions. """


def list_models(file_name):
    """ list all stored models in given file.

    Parameters
    ----------
    file_name: str
        path to file to list models for

    Returns
    -------
    dict: {model_name: {'repr' : 'string representation, 'created': 'human readable date', ...}

    """
    import h5py
    with h5py.File(file_name, mode='r') as f:
        return {k: {'repr': f[k].attrs['class_str'],
                    'created': f[k].attrs['created_readable'],
                    'saved_streaming_chain': f[k].attrs['saved_streaming_chain']
                    } for k in f.keys()}


def save(obj, file_name, model_name='latest', save_streaming_chain=False):
    import h5py
    import time
    from jsonpickle.pickler import Pickler

    global _handlers_registered
    if not _handlers_registered:
        _reg_all_handlers()
        _handlers_registered = True
    # if we are serializing a pipeline element, store whether to store the chain elements.
    old_flag = obj._save_data_producer
    obj._save_data_producer = save_streaming_chain
    assert obj._save_data_producer == save_streaming_chain
    try:
        with h5py.File(file_name) as f:
            g = f.require_group(str(model_name))
            g.attrs['created'] = time.time()
            g.attrs['created_readable'] = time.asctime()
            g.attrs['class_str'] = str(obj)
            g.attrs['class_repr'] = repr(obj)
            g.attrs['saved_streaming_chain'] = save_streaming_chain
            # now encode the object (this will write all numpy arrays to current group).
            context = Pickler()
            context.h5_file = g
            # array id provider (simple counter)
            from itertools import count
            context.next_array_id = count(0)

            flattened = jsonpickle.pickler.encode(obj, context=context, warn=True)
            # attach the json string in the H5 file.
            g.attrs['model'] = flattened
    except Exception as e:
        if isinstance(obj, Loggable):
            obj.logger.exception('During saving the object ("{error}") '
                                 'the following error occurred'.format(error=e))
        raise
    finally:
        # restore old state.
        obj._save_data_producer = old_flag


def load(file_name, model_name='latest'):
    """ loads a previously saved object of this class from a file.

    Parameters
    ----------
    file_name : str or file like object (has to provide read method).
        The file like object tried to be read for a serialized object.
    model_name: str, default='latest'
        if multiple versions are contained in the file, older versions can be accessed by
        their name. Use func:`list_models` to get a representation of all stored models.

    Returns
    -------
    obj : the de-serialized object
    """

    import h5py
    with h5py.File(file_name, 'r') as f:
        if model_name not in f:
            raise ValueError('Model with name "{model_name}" not found in given file {file_name}'
                             .format(model_name=model_name, file_name=file_name))
        group = f[model_name]
        inp = group.attrs['model']
        inp = class_rename_registry.upgrade_old_names_in_json(inp)
        global _handlers_registered
        if not _handlers_registered:
            _reg_all_handlers()
            _handlers_registered = True

        # we pass the hdf5 file handle to the unpickler by adding a known attribute.
        from jsonpickle.unpickler import Unpickler
        context = Unpickler()
        context.h5_file = group
        obj = jsonpickle.unpickler.decode(inp, context=context)

        return obj


class SerializableMixIn(object):
    """ Base class of serializable classes using get/set_state.

    Derive from this class to make your class serializable. Do not forget to
    add a version number to your class to distinguish old and new copies of the
    source code. The static attribute '_serialize_fields' is a iterable of names,
    which are preserved during serialization.

    To aid the process of loading old models in a new version of the software, there
    is the the static field '_serialize_interpolation_map', which is a mapping from
    old version number to a set of operations to transform the old class state to the
    recent version of the class.

    Valid operations are:
    1. ('rm', 'name') -> delete the attribute with given name.
    2. ('mv', 'old', 'new') -> rename the attribute from 'old' to 'new'.
    3. ('set', 'name', value) -> set an attribute with name 'name' to given value.
    4. ('map', 'name', func) -> apply the function 'func' to attribute 'name'. The function
      should accept one argument, namely the attribute and return the new value for it.

    Similar to map, there are two callbacks to hook into the serialization process:
    5. ('set_state_hook', func) -> a function which may transform the state dictionary
       before __getstate__ returns.

    Example
    -------

    >>> import pyemma
    >>> from pyemma.util.contexts import named_temporary_file
    >>> class MyClass(SerializableMixIn):
    ...    _serialize_version = 0
    ...    _serialize_fields = ['x']
    ...    def __init__(self, x=42):
    ...        self.x = x

    >>> inst = MyClass()
    >>> with named_temporary_file() as file:
    ...    inst.save(file)
    ...    inst_restored = pyemma.load(file)
    >>> assert inst_restored.x == inst.x # doctest: +SKIP
    # skipped because MyClass is not importable.

    """

    _serialize_fields = ()
    """ attribute names to serialize """

    def __new__(cls, *args, **kwargs):
        assert cls != SerializableMixIn.__class__
        if not hasattr(cls, '_serialize_version'):
            raise DeveloperError('your class {cls} does not have a _serialize_version field!')

        res = super(SerializableMixIn, cls).__new__(cls)
        return res

    def save(self, file_name, model_name='latest', save_streaming_chain=False):
        r"""
        Parameters
        -----------
        file_name: str
            path to desired output file
        model_name: str, default=latest
            creates a group named 'model_name' in the given file, which will contain all of the data.
            If the name already exists,
        save_streaming_chain : boolean, default=False
            if True, the data_producer(s) of this object will also be saved in the given file.

        Examples
        --------
        >>> import pyemma, numpy as np, pprint
        >>> from pyemma.util.contexts import named_temporary_file
        >>> m = pyemma.msm.MSM(P=np.array([[0.1, 0.9], [0.9, 0.1]]))

        >>> with named_temporary_file() as file: # doctest: +ELLIPSIS,+NORMALIZE_WHITESPACE
        ...    m.save(file, 'simple')
        ...    pprint.pprint(list_models(file))
        ...    inst_restored = pyemma.load(file, 'simple')
           {'simple': {'created': '...',
                'repr': 'MSM(P=array([[ 0.1,  0.9],\n'
                        "       [ 0.9,  0.1]]), dt_model='1 step', neig=2,\n"
                        '  pi=array([ 0.5,  0.5]), reversible=True)'...}}
        >>> assert np.all(inst_restored.P == m.P)
        """
        return save(self, file_name, model_name, save_streaming_chain)

    @classmethod
    def load(cls, file_name, model_name='latest'):
        """ loads a previously saved object of this class from a file.

        Parameters
        ----------
        file_name : str or file like object (has to provide read method).
            The file like object tried to be read for a serialized object.
        model_name: str, default='latest'
            if multiple versions are contained in the file, older versions can be accessed by
            their name. Use func:list_models to get a representation of all stored models.

        Returns
        -------
        obj : the de-serialized object
        """
        obj = load(file_name, model_name)

        if obj.__class__ != cls:
            raise ValueError("Given file '%s' did not contain the right type:"
                             " desired(%s) vs. actual(%s)" % (file_name, cls, obj.__class__))
        if not hasattr(cls, '_serialize_version'):
            raise DeveloperError("your class does not implement the serialization protocol of PyEMMA.")

        return obj

    @property
    def _save_data_producer(self):
        try:
            return self.__save_data_producer
        except AttributeError:
            self.__save_data_producer = False
        return self.__save_data_producer

    @_save_data_producer.setter
    def _save_data_producer(self, value):
        self.__save_data_producer = value
        # forward flag to the next data producer
        if (value and
            hasattr(self, 'data_producer') and self.data_producer and self.data_producer is not self):
            # ensure the data_producer is serializable
            if not hasattr(self.data_producer.__class__, '_serialize_version'):
                raise RuntimeError('class in chain is not serializable: {}'.format(self.data_producer.__class__))
            self.data_producer._save_data_producer = value

    def _get_state_of_serializeable_fields(self, klass):
        """ :return a dictionary {k:v} for k in self.serialize_fields and v=getattr(self, k)"""
        res = {}
        assert all(isinstance(f, str) for f in klass._serialize_fields)
        for field in klass._serialize_fields:
            # only try to get fields, we actually have.
            if hasattr(self, field):
                res[field] = getattr(self, field)
        return res

    def _validate_interpolation_map(self, klass):
        # version numbers should be sorted
        from collections import OrderedDict
        inter_map = OrderedDict(sorted(klass._serialize_interpolation_map.items()))
        if _debug:
            logger.debug("validate map: %s", inter_map)

        # check for valid operations: add, rm, mv, map
        valid_ops = ('set', 'rm', 'mv', 'map')
        for k, v in inter_map.items():
            if not is_int(k):
                raise DeveloperError("all keys of _serialize_interpolation_map "
                                     "have to be of type int (class version numbers)")
            if not isinstance(v, (list, tuple)):
                raise DeveloperError("actions per version have to be list or tuple")

            for action in v:
                if action[0] not in valid_ops:
                    raise DeveloperError("Your _serialize_interpolation_map contains invalid operations. "
                                         "Valid ops are: {valid_ops}. You provided {provided}"
                                         .format(valid_ops=valid_ops, provided=action[0]))

        klass._serialize_interpolation_map = inter_map

    def __interpolate(self, state, klass):
        # First lookup the version of klass in the state (this maps from old versions too).
        # Lookup attributes in interpolation map according to version number of the class.
        # Drag in all prior versions attributes
        if not hasattr(klass, '_serialize_interpolation_map'):
            return

        klass_version = self._get_version_for_class_from_state(state, klass)

        if klass_version > klass._serialize_version:
            return

        self._validate_interpolation_map(klass)

        if _debug:
            logger.debug("input state: %s" % state)
        for key in klass._serialize_interpolation_map.keys():
            if not (klass._serialize_version > key >= klass_version):
                if _debug:
                    logger.debug("skipped interpolation rules for version %s" % key)
                continue
            if _debug:
                logger.debug("processing rules for version %s" % key)
            actions = klass._serialize_interpolation_map[key]
            for a in actions:
                if _debug:
                    logger.debug("processing rule: %s", str(a))
                if len(a) == 3:
                    operation, name, value = a
                    if operation == 'set':
                        state[name] = value
                    elif operation == 'mv':
                        try:
                            arg = state.pop(name)
                            state[value] = arg
                        except KeyError:
                            raise DeveloperError("the previous version didn't "
                                                 "store an attribute named '{}'".format(a[1]))
                    elif operation == 'map':
                        func = value
                        if hasattr(func, '__func__'):
                            func = func.__func__
                        assert callable(func)
                        state[name] = func(state[name])
                elif len(a) == 2:
                    action, value = a
                    if action == 'rm':
                        state.pop(value, None)
        if _debug:
            logger.debug("interpolated state: %s", state)

    @staticmethod
    def _get_version_for_class_from_state(state, klass):
        """ retrieves the version of the current klass from the state mapping from old locations to new ones. """
        # klass may have renamed, so we have to look this up in the class rename registry.
        names = [_importable_name(klass)]
        # lookup old names, handled by current klass.
        names.extend(class_rename_registry.old_handled_by(klass))
        for n in names:
            try:
                return state['class_tree_versions'][n]
            except KeyError:
                continue
        # if we did not find a suitable version number return infinity.
        return float('inf')

    def _set_state_from_serializeable_fields_and_state(self, state, klass):
        """ set only fields from state, which are present in klass._serialize_fields """
        if _debug:
            logger.debug("restoring state for class %s", klass)

        # handle field renames, deletion, transformations etc.
        klass.__interpolate(self, state, klass)

        if hasattr(klass, '_get_param_names'):
            for param in klass._get_param_names():
                if param in state:
                    setattr(self, param, state.pop(param))

        for field in klass._serialize_fields:
            if field in state:
                setattr(self, field, state.pop(field))
            else:
                if _debug:
                    logger.debug("skipped %s, because it is not declared in _serialize_fields", field)

    def __getstate__(self):
        # We just dump the version number for comparison with the actual class.
        # Note: we do not want to set the version number in __setstate__,
        # since we obtain it from the actual definition.
        try:
            if _debug:
                logger.debug('get state of %s' % self)
            if not hasattr(self, '_serialize_version'):
                raise DeveloperError('The "{klass}" should define a static "_serialize_version" attribute.'
                                     .format(klass=self.__class__))
            res = {}
            # currently it is used to handle class renames etc.
            res['class_tree_versions'] = {}
            for c in self.__class__.mro():
                name = _importable_name(c)
                if hasattr(c, '_serialize_version'):
                    v = c._serialize_version
                else:
                    v = -1
                res['class_tree_versions'][name] = v

            # if we want to save the chain, do this now:
            if self._save_data_producer:
                assert hasattr(self, 'data_producer')
                res['data_producer'] = self.data_producer

            # In case of of a Reader (primary DataSource), we need to store this hidden attribute.
            if hasattr(self, '_is_reader'):
                res['_is_reader'] = self._is_reader

            classes_to_inspect = self._get_classes_to_inspect()
            if _debug:
                logger.debug("classes to inspect during setstate: \n%s" % classes_to_inspect)
            from pyemma._ext.sklearn.base import BaseEstimator
            for klass in classes_to_inspect:
                inc = self._get_state_of_serializeable_fields(klass)
                # get estimation parameter for all classes in the hierarchy too.
                if issubclass(klass, BaseEstimator):
                    up = {k: getattr(self, k, None) for k in klass._get_param_names()}
                    inc.update(up)
                res.update(inc)

            # handle special cases Estimator and Model, just use their parameters.
            if hasattr(self, 'get_params'):
                #res.update(self.get_params())
                # remember if it has been estimated.
                res['_estimated'] = self._estimated
                try:
                    res['model'] = self._model
                except AttributeError:
                    pass

            # handle model state
            if hasattr(self, 'get_model_params'):
                state = self.get_model_params(deep=False)
                res.update(state)

            # store the current software version
            from pyemma import version
            res['_pyemma_version'] = version

            return res
        except:
            logger.exception('exception during pickling {}'.format(self))

    def __setstate__(self, state):
        try:
            assert state
            # handle exceptions here, because they will be sucked up by jsonpickle and silently fail...
            classes_to_inspect = self._get_classes_to_inspect()

            if hasattr(self, 'set_params') and hasattr(self, '_get_param_names'):
                self._estimated = state.pop('_estimated')
                model = state.pop('model', None)
                self._model = model

            from pyemma._ext.sklearn.base import BaseEstimator
            for klass in classes_to_inspect:
                self._set_state_from_serializeable_fields_and_state(state, klass=klass)

            if hasattr(self, 'set_model_params') and hasattr(self, '_get_model_param_names'):
                # only apply params suitable for the current model
                names = self._get_model_param_names()
                new_state = {key: state.pop(key) for key in names if key in state}

                self.set_model_params(**new_state)

            if hasattr(self, 'data_producer') and 'data_producer' in state:
                self.data_producer = state['data_producer']

            if '_is_reader' in state:
                self._is_reader = state.pop('_is_reader')

            self._pyemma_version = state.pop('_pyemma_version', '!!!! UNKNOWN !!!!')
            state.pop('class_tree_versions')
            assert len(state) == 0, 'unhandled attributes in state'
        except AssertionError:
            import pprint
            logger.error('left-overs after setstate: %s', pprint.pformat(state))
        except:
            logger.exception('exception during pickling {}'.format(self))

    def _get_classes_to_inspect(self):
        """ gets classes self derives from which
         1. are Estimators (or sub classes)
         2. have custom fields (_serialize_fields
         """
        classes_with_custom_fields = [c for c in self.__class__.mro() if
                hasattr(c, '_serialize_fields') and c._serialize_fields]
        # sub classes of Estimator (base estimator might have their own parameters each
        estimator_classes = [c for c in self.__class__.mro() if (hasattr(c, '_get_param_names')
                             and hasattr(c, '_serialize_version'))]
        return classes_with_custom_fields + estimator_classes

    def __init_subclass__(self, *args, **kwargs):
        # ensure, that if this is subclasses, we have a proper class version.
        if not hasattr(self, '_serialize_version'):
            raise DeveloperError('{} does not have field _serialize_version'.format(self))
