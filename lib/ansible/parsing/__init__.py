# (c) 2012-2014, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

# Make coding more python3-ish
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import copy
import json
import os

from yaml import load, YAMLError

from ansible.errors import AnsibleParserError
from ansible.errors.yaml_strings import YAML_SYNTAX_ERROR
from ansible.parsing.vault import VaultLib
from ansible.parsing.splitter import unquote
from ansible.parsing.yaml.loader import AnsibleLoader
from ansible.parsing.yaml.objects import AnsibleBaseYAMLObject, AnsibleUnicode
from ansible.utils.path import unfrackpath
from ansible.utils.unicode import to_unicode

class DataLoader():

    '''
    The DataLoader class is used to load and parse YAML or JSON content,
    either from a given file name or from a string that was previously
    read in through other means. A Vault password can be specified, and
    any vault-encrypted files will be decrypted.

    Data read from files will also be cached, so the file will never be
    read from disk more than once.

    Usage:

        dl = DataLoader()
        (or)
        dl = DataLoader(vault_password='foo')

        ds = dl.load('...')
        ds = dl.load_from_file('/path/to/file')
    '''

    def __init__(self, vault_password=None):
        self._basedir = '.'
        self._vault_password = vault_password
        self._FILE_CACHE = dict()

        self._vault = VaultLib(password=vault_password)

    def load(self, data, file_name='<string>', show_content=True):
        '''
        Creates a python datastructure from the given data, which can be either
        a JSON or YAML string. 
        '''

        try:
            # we first try to load this data as JSON
            return json.loads(data)
        except:
            # if loading JSON failed for any reason, we go ahead
            # and try to parse it as YAML instead

            if isinstance(data, AnsibleUnicode):
                # The PyYAML's libyaml bindings use PyUnicode_CheckExact so
                # they are unable to cope with our subclass.
                # Unwrap and re-wrap the unicode so we can keep track of line
                # numbers
                new_data = unicode(data)
            else:
                new_data = data
            try:
                new_data = self._safe_load(new_data, file_name=file_name)
            except YAMLError as yaml_exc:
                self._handle_error(yaml_exc, file_name, show_content)

            if isinstance(data, AnsibleUnicode):
                new_data = AnsibleUnicode(new_data)
                new_data.ansible_pos = data.ansible_pos
            return new_data

    def load_from_file(self, file_name):
        ''' Loads data from a file, which can contain either JSON or YAML.  '''

        file_name = self.path_dwim(file_name)

        # if the file has already been read in and cached, we'll
        # return those results to avoid more file/vault operations
        if file_name in self._FILE_CACHE:
            parsed_data = self._FILE_CACHE[file_name]
        else:
            # read the file contents and load the data structure from them
            (file_data, show_content) = self._get_file_contents(file_name)
            parsed_data = self.load(data=file_data, file_name=file_name, show_content=show_content)

            # cache the file contents for next time
            self._FILE_CACHE[file_name] = parsed_data

        # return a deep copy here, so the cache is not affected
        return copy.deepcopy(parsed_data)

    def path_exists(self, path):
        path = self.path_dwim(path)
        return os.path.exists(path)

    def is_file(self, path):
        path = self.path_dwim(path)
        return os.path.isfile(path)

    def is_directory(self, path):
        path = self.path_dwim(path)
        return os.path.isdir(path)

    def list_directory(self, path):
        path = self.path_dwim(path)
        return os.listdir(path)

    def _safe_load(self, stream, file_name=None):
        ''' Implements yaml.safe_load(), except using our custom loader class. '''

        loader = AnsibleLoader(stream, file_name)
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()

    def _get_file_contents(self, file_name):
        '''
        Reads the file contents from the given file name, and will decrypt them
        if they are found to be vault-encrypted.
        '''
        if not file_name or not isinstance(file_name, basestring):
            raise AnsibleParserError("Invalid filename: '%s'" % str(file_name))

        if not self.path_exists(file_name) or not self.is_file(file_name):
            raise AnsibleParserError("the file_name '%s' does not exist, or is not readable" % file_name)

        show_content = True
        try:
            with open(file_name, 'rb') as f:
                data = f.read()
                if self._vault.is_encrypted(data):
                    data = self._vault.decrypt(data)
                    show_content = False

            data = to_unicode(data, errors='strict')
            return (data, show_content)

        except (IOError, OSError) as e:
            raise AnsibleParserError("an error occurred while trying to read the file '%s': %s" % (file_name, str(e)))

    def _handle_error(self, yaml_exc, file_name, show_content):
        '''
        Optionally constructs an object (AnsibleBaseYAMLObject) to encapsulate the
        file name/position where a YAML exception occurred, and raises an AnsibleParserError
        to display the syntax exception information.
        '''

        # if the YAML exception contains a problem mark, use it to construct
        # an object the error class can use to display the faulty line
        err_obj = None
        if hasattr(yaml_exc, 'problem_mark'):
            err_obj = AnsibleBaseYAMLObject()
            err_obj.ansible_pos = (file_name, yaml_exc.problem_mark.line + 1, yaml_exc.problem_mark.column + 1)

        raise AnsibleParserError(YAML_SYNTAX_ERROR, obj=err_obj, show_content=show_content)

    def get_basedir(self):
        ''' returns the current basedir '''
        return self._basedir

    def set_basedir(self, basedir):
        ''' sets the base directory, used to find files when a relative path is given '''

        if basedir is not None:
            self._basedir = to_unicode(basedir)

    def path_dwim(self, given):
        '''
        make relative paths work like folks expect.
        '''

        given = unquote(given)

        if given.startswith("/"):
            return os.path.abspath(given)
        elif given.startswith("~"):
            return os.path.abspath(os.path.expanduser(given))
        else:
            return os.path.abspath(os.path.join(self._basedir, given))

    def path_dwim_relative(self, path, dirname, source):
        ''' find one file in a role/playbook dirs with/without dirname subdir '''

        search = []
        isrole = False

        # I have full path, nothing else needs to be looked at
        if source.startswith('~') or source.startswith('/'):
            search.append(self.path_dwim(source))
        else:
            # base role/play path + templates/files/vars + relative filename
            search.append(os.path.join(path, dirname, source))

            basedir = unfrackpath(path)

            # is it a role and if so make sure you get correct base path
            if path.endswith('tasks') and os.path.exists(os.path.join(path,'main.yml')) \
                or os.path.exists(os.path.join(path,'tasks/main.yml')):
                isrole = True
                if path.endswith('tasks'):
                    basedir = unfrackpath(os.path.dirname(path))

            cur_basedir = self._basedir
            self.set_basedir(basedir)
            # resolved base role/play path + templates/files/vars + relative filename
            search.append(self.path_dwim(os.path.join(basedir, dirname, source)))
            self.set_basedir(cur_basedir)

            if isrole and not source.endswith(dirname):
                # look in role's tasks dir w/o dirname
                search.append(self.path_dwim(os.path.join(basedir, 'tasks', source)))

            # try to create absolute path for loader basedir + templates/files/vars + filename
            search.append(self.path_dwim(os.path.join(dirname,source)))
            search.append(self.path_dwim(os.path.join(basedir, source)))

            # try to create absolute path for loader basedir + filename
            search.append(self.path_dwim(source))

        for candidate in search:
            if os.path.exists(candidate):
                break

        return candidate

