#!/usr/bin/env python

"""
This module defines a Drone to assimilate vasp data and insert it into a
Mongo database.
"""

from __future__ import division

__author__ = "Shyue Ping Ong"
__copyright__ = "Copyright 2012, The Materials Project"
__version__ = "2.0.0"
__maintainer__ = "Shyue Ping Ong"
__email__ = "shyue@mit.edu"
__date__ = "Mar 18, 2012"

import os
import re
import glob
import logging
import datetime
import string
import json
import socket
from collections import OrderedDict

from pymongo import MongoClient
import gridfs

from pymatgen.apps.borg.hive import AbstractDrone
from pymatgen.analysis.structure_analyzer import VoronoiCoordFinder
from pymatgen.core.structure import Structure
from pymatgen.io.vaspio import Vasprun, Incar, Kpoints, Potcar, Poscar, \
    Outcar, Oszicar
from pymatgen.io.cifio import CifWriter
from pymatgen.symmetry.finder import SymmetryFinder
from pymatgen.analysis.bond_valence import BVAnalyzer
from pymatgen.util.io_utils import zopen


logger = logging.getLogger(__name__)


class VaspToDbTaskDrone(AbstractDrone):
    """
    VaspToDbDictDrone assimilates directories containing vasp input to
    inserted db tasks. There are some restrictions on the valid directory
    structures:

    1. There can be only one vasp run in each directory. Nested directories
       are fine.
    2. Directories designated "relax1", "relax2" are considered to be 2 parts
       of an aflow style run.
    3. Directories containing vasp output with ".relax1" and ".relax2" are
       also considered as 2 parts of an aflow style run.
    """
    vasprun_pattern = re.compile("^vasprun.xml([\w\.]*)")

    """
    Version of this db creator document. As the document structure is updated,
    so should this version number (other scripts could depend on it for
    parsing)
    """
    __version__ = "2.0.0"

    def __init__(self, host="127.0.0.1", port=27017, database="vasp",
                 user=None, password=None,  parse_dos=False,
                 simulate_mode=False, collection='tasks',
                 additional_fields=None, update_duplicates=True):
        """
        Args:
            host:
                Hostname of database machine.
            database:
                Actual database to access.
            user:
                User for db access. Requires write access.
            password:
                Password for db access. Requires write access.
            port:
                Port for db access. Defaults to mongo's default of 27017.
            parse_dos:
                Whether to parse the DOS data where possible. Defaults to
                False.
            simulate_mode:
                Allows one to simulate db insertion without actually performing
                the insertion.
            collection:
                Collection to insert to.
            additional_fields:
                Dict specifying additional fields to append to each doc
                inserted into the collection. For example, allows one to add
                an author or tags to a whole set of runs for example.
            update_duplicates:
                If True, if a duplicate path exists in the collection, the
                entire doc is updated. Else, duplicates are skipped.
        """
        self._host = host
        self._database = database
        self._user = user
        self._password = password
        self._collection = collection
        self._port = port
        self._simulate = simulate_mode
        self._parse_dos = parse_dos
        self._additional_fields = {} if not additional_fields \
            else additional_fields
        self._update_duplicates = update_duplicates

    def assimilate(self, path):
        """
        Parses vasp runs. Then insert the result into the db. and return the
        task_id or doc of the insertion.

        Returns:
            If in simulate_mode, the entire doc is returned for debugging
            purposes. Else, only the task_id of the inserted doc is returned.
        """
        try:
            d = self.get_task_doc(path, self._parse_dos,
                                  self._additional_fields)
            tid = self._insert_doc(d)
            return tid
        except Exception as ex:
            import traceback
            print traceback.format_exc(ex)
            logger.error(traceback.format_exc(ex))
            return False

    @classmethod
    def get_task_doc(cls, path, parse_dos=False, additional_fields=None):
        """
        Get the entire task doc for a path, including any post-processing.
        """
        logger.info("Getting task doc for base dir :{}".format(path))

        d = None
        vasprun_files = OrderedDict()
        files = os.listdir(path)
        if ('relax1' in files and 'relax2' in files and
                os.path.isdir(os.path.join(path, 'relax1')) and
                os.path.isdir(os.path.join(path, 'relax2'))):
            #Materials project style aflow runs.
            for subtask in ['relax1', 'relax2']:
                for f in os.listdir(os.path.join(path, subtask)):
                    if VaspToDbTaskDrone.vasprun_pattern.match(f):
                            vasprun_files[subtask] = os.path.join(subtask, f)
        elif 'STOPCAR' in files:
            #Stopped runs. Try to parse as much as possible.
            logger.info(path + " contains stopped run")
            for subtask in ['relax1', 'relax2']:
                if subtask in files and \
                        os.path.isdir(os.path.join(path, subtask)):
                    for f in os.listdir(os.path.join(path, subtask)):
                        if VaspToDbTaskDrone.vasprun_pattern.match(f):
                            vasprun_files[subtask] = os.path.join(
                                subtask, f)
        else:
            for f in files:
                m = VaspToDbTaskDrone.vasprun_pattern.match(f)
                if m:
                    fileext = m.group(1)
                    if fileext.startswith(".relax2"):
                        fileext = "relax2"
                    elif fileext.startswith(".relax1"):
                        fileext = "relax1"
                    else:
                        fileext = "standard"
                    vasprun_files[fileext] = f

        #Need to sort so that relax1 comes before relax2.
        sorted_vasprun_files = OrderedDict()
        for k in sorted(vasprun_files.keys()):
            sorted_vasprun_files[k] = vasprun_files[k]

        if len(vasprun_files) > 0:
            d = cls.generate_doc(path, vasprun_files, parse_dos,
                                 additional_fields)
            if not d:
                d = cls.process_killed_run(path)
            cls.post_process(path, d)
        elif (not (path.endswith('relax1') or
              path.endswith('relax2'))) and contains_vasp_input(path):
            #If not Materials Project style, process as a killed run.
            logger.warning(path + " contains killed run")
            d = cls.process_killed_run(path)
            cls.post_process(path, d)

        return d

    def _insert_doc(self, d):
        if not self._simulate:
            # Perform actual insertion into db. Because db connections cannot
            # be pickled, every insertion needs to create a new connection
            # to the db.
            conn = MongoClient(self._host, self._port)
            db = conn[self._database]
            if self._user:
                db.authenticate(self._user, self._password)
            coll = db[self._collection]

            # Insert dos data into gridfs and then remove it from the dict.
            # DOS data tends to be above the 4Mb limit for mongo docs. A ref
            # to the dos file is in the dos_fs_id.
            result = coll.find_one({'dir_name': d['dir_name']},
                                   fields=['dir_name', 'task_id'])
            if result is None or self._update_duplicates:
                if self._parse_dos and 'calculations' in d:
                    for calc in d['calculations']:
                        if 'dos' in calc:
                            dos = json.dumps(calc['dos'])
                            if not self._simulate:
                                fs = gridfs.GridFS(db, 'dos_fs')
                                dosid = fs.put(dos)
                                calc['dos_fs_id'] = dosid
                                del calc['dos']
                            else:
                                logger.info("Simulated Insert DOS into db.")

                d['last_updated'] = datetime.datetime.today()
                if result is None:
                    if ('task_id' not in d) or (not d['task_id']):
                        if db.counter.find({"_id": "taskid"}).count() == 0:
                            db.counter.insert({"_id": "taskid", "c": 1})
                        d['task_id'] = db.counter.find_and_modify(
                            query={'_id': "taskid"},
                            update={'$inc': {'c': 1}}
                        )['c']
                    logger.info("Inserting {} with taskid = {}"
                                .format(d['dir_name'], d['task_id']))
                    coll.insert(d, safe=True)
                elif self._update_duplicates:
                    d['task_id'] = result['task_id']
                    logger.info("Updating {} with taskid = {}"
                                .format(d['dir_name'], d['task_id']))
                    coll.update({'dir_name': d['dir_name']}, {'$set': d})
                return d['task_id']
            else:
                logger.info("Skipping duplicate {}".format(d['dir_name']))
        else:
            d["task_id"] = 0
            logger.info("Simulated Insert into database for {} with task_id {}"
                        .format(d['dir_name'], d['task_id']))
            return d

    @classmethod
    def post_process(cls, mydir, d):
        """
        Postprocessing added by Anubhav Jain 7/20/2012
        """
        logger.info("Post-processing dir:{}".format(mydir))

        fullpath = os.path.abspath(mydir)
        transformations = {}
        filenames = glob.glob(os.path.join(fullpath, "transformations.json*"))
        if len(filenames) >= 1:
            # Handles the new style transformations file.
            with zopen(filenames[0], "rb") as f:
                transformations = json.load(f)
                try:
                    m = re.match("(\d+)-ICSD",
                                 transformations["history"][0]["source"])
                    if m:
                        d["icsd_id"] = int(m.group(1))
                except ValueError:
                    pass

        else:
            logger.warning("Transformations file does not exist.")

        other_parameters = transformations.get("other_parameters")
        new_tags = None
        if other_parameters:
            # We don't want to leave tags or authors in the
            # transformations file because they'd be copied into
            # every structure generated after this one.
            new_tags = other_parameters.pop("tags", None)
            new_author = other_parameters.pop("author", None)
            if new_author:
                d["author"] = new_author
            if not other_parameters:  # if dict is now empty remove it
                transformations.pop("other_parameters")

        d["transformations"] = transformations

        # Parse OUTCAR for additional information and run stats.
        run_stats = {}
        for filename in glob.glob(os.path.join(fullpath, "OUTCAR*")):
            outcar = Outcar(filename)
            i = 1 if re.search("relax2", filename) else 0
            taskname = "relax2" if re.search("relax2", filename) else "relax1"
            d["calculations"][i]["output"]["outcar"] = outcar.to_dict
            run_stats[taskname] = outcar.run_stats

        try:
            overall_run_stats = {}
            for key in ["Total CPU time used (sec)", "User time (sec)",
                        "System time (sec)", "Elapsed time (sec)"]:
                overall_run_stats[key] = sum([v[key]
                                              for v in run_stats.values()])
            run_stats["overall"] = overall_run_stats
        except:
            logger.error("Bad run stats for {}.".format(fullpath))

        d["run_stats"] = run_stats

        d["dir_name"] = get_uri(mydir)

        if new_tags:
            d["tags"] = new_tags

        logger.info("Post-processed " + fullpath)

    @classmethod
    def process_killed_run(cls, dirname):
        """
        Process a killed vasp run.
        """
        fullpath = os.path.abspath(dirname)
        logger.info("Processing Killed run " + fullpath)
        d = {'dir_name': fullpath, 'state': 'killed', 'oszicar': {}}

        for f in os.listdir(dirname):
            filename = os.path.join(dirname, f)
            if re.match("INCAR.*", f):
                try:
                    incar = Incar.from_file(filename)
                    d['incar'] = incar.to_dict
                    d['is_hubbard'] = incar.get('LDAU', False)
                    if d['is_hubbard']:
                        us = incar.get('LDAUU', [])
                        js = incar.get('LDAUJ', [])
                        if sum(us) == 0 and sum(js) == 0:
                            d['is_hubbard'] = False
                            d['hubbards'] = {}
                    else:
                        d['hubbards'] = {}
                    if d['is_hubbard']:
                        d['run_type'] = "GGA+U"
                    elif incar.get('LHFCALC', False):
                        d['run_type'] = "HF"
                    else:
                        d['run_type'] = "GGA"
                except Exception as ex:
                    print str(ex)
                    logger.error('Unable to parse INCAR for killed run {}.'
                                 .format(dirname))
            elif re.match("KPOINTS.*", f):
                try:
                    kpoints = Kpoints.from_file(filename)
                    d['kpoints'] = kpoints.to_dict
                except:
                    logger.error('Unable to parse KPOINTS for killed run {}.'
                                 .format(dirname))
            elif re.match("POSCAR.*", f):
                try:
                    s = Poscar.from_file(filename).structure
                    comp = s.composition
                    el_amt = s.composition.get_el_amt_dict()
                    d.update({'unit_cell_formula': comp.to_dict,
                              'reduced_cell_formula': comp.to_reduced_dict,
                              'elements': list(el_amt.keys()),
                              'nelements': len(el_amt),
                              'pretty_formula': comp.reduced_formula,
                              'anonymous_formula': comp.anonymized_formula,
                              'nsites': comp.num_atoms,
                              'chemsys': "-".join(sorted(el_amt.keys()))})
                    d['poscar'] = s.to_dict
                except:
                    logger.error('Unable to parse POSCAR for killed run {}.'
                                 .format(dirname))
            elif re.match("POTCAR.*", f):
                try:
                    potcar = Potcar.from_file(filename)
                    d['pseudo_potential'] = {'functional': 'pbe',
                                             'pot_type': 'paw',
                                             'labels': potcar.symbols}
                except:
                    logger.error('Unable to parse POTCAR for killed run in {}.'
                                 .format(dirname))
            elif re.match('OSZICAR', f):
                try:
                    d['oszicar']['root'] = \
                        Oszicar(os.path.join(dirname, f)).to_dict
                except:
                    logger.error('Unable to parse OSZICAR for killed run in {}.'
                                 .format(dirname))
            elif re.match('relax\d', f):
                if os.path.exists(os.path.join(dirname, f, 'OSZICAR')):
                    try:
                        d['oszicar'][f] = Oszicar(
                            os.path.join(dirname, f, 'OSZICAR')).to_dict
                    except:
                        logger.error('Unable to parse OSZICAR for killed '
                                     'run in {}.'.format(dirname))
        return d

    @classmethod
    def process_vasprun(cls, dirname, taskname, filename, parse_dos):
        """
        Process a vasprun.xml file.
        """
        vasprun_file = os.path.join(dirname, filename)
        r = Vasprun(vasprun_file)
        d = r.to_dict
        d['dir_name'] = os.path.abspath(dirname)
        d['completed_at'] = \
            str(datetime.datetime.fromtimestamp(os.path.getmtime(
                vasprun_file)))
        d['cif'] = str(CifWriter(r.final_structure))
        d['density'] = r.final_structure.density
        if parse_dos:
            try:
                d['dos'] = r.complete_dos.to_dict
            except Exception:
                logger.warn("No valid dos data exist in {}.\n Skipping dos"
                            .format(dirname))
        if taskname == 'relax1' or taskname == 'relax2':
            d['task'] = {'type': "aflow", "name": taskname}
        else:
            d['task'] = {'type': "standard", "name": "standard"}
        return d

    @classmethod
    def generate_doc(cls, dirname, vasprun_files, parse_dos,
                     additional_fields):
        """
        Process aflow style runs, where each run is actually a combination of
        two vasp runs.
        """
        try:
            fullpath = os.path.abspath(dirname)
            #Defensively copy the additional fields first.  This is a MUST.
            #Otherwise, parallel updates will see the same object and inserts
            #will be overridden!!
            d = {k: v for k, v in additional_fields.items()} \
                if additional_fields else {}
            d['dir_name'] = fullpath
            d['schema_version'] = VaspToDbTaskDrone.__version__
            d['calculations'] = [cls.process_vasprun(dirname, taskname,
                                                     filename, parse_dos)
                                 for taskname, filename
                                 in vasprun_files.items()]
            d1 = d['calculations'][0]
            d2 = d['calculations'][-1]
            for root_key in ['completed_at', 'nsites', 'unit_cell_formula',
                             'reduced_cell_formula', 'pretty_formula',
                             'elements', 'nelements', 'cif', 'density',
                             'is_hubbard', 'hubbards', 'run_type']:
                d[root_key] = d2[root_key]
            d['chemsys'] = '-'.join(sorted(d2['elements']))
            d['input'] = {'crystal': d1['input']['crystal']}
            vals = sorted(d2['reduced_cell_formula'].values())
            d['anonymous_formula'] = {string.ascii_uppercase[i]: float(vals[i])
                                      for i in xrange(len(vals))}
            d['output'] = {'crystal': d2['output']['crystal'],
                           'final_energy': d2['output']['final_energy'],
                           'final_energy_per_atom': d2['output']
                           ['final_energy_per_atom']}
            d['name'] = 'aflow'
            d['pseudo_potential'] = {'functional': 'pbe', 'pot_type': 'paw',
                                     'labels': d2['input']['potcar']}

            if len(d['calculations']) == 2 or \
                    vasprun_files.keys()[0] != "relax1":
                d['state'] = 'successful' if d2['has_vasp_completed'] \
                    else 'unsuccessful'
            else:
                d['state'] = 'stopped'
            d['analysis'] = get_basic_analysis_and_error_checks(d)

            sg = SymmetryFinder(Structure.from_dict(d['output']['crystal']),
                                0.1)
            d['spacegroup'] = {'symbol': sg.get_spacegroup_symbol(),
                               'number': sg.get_spacegroup_number(),
                               'point_group': unicode(sg.get_point_group(),
                                                      errors='ignore'),
                               'source': 'spglib',
                               'crystal_system': sg.get_crystal_system(),
                               'hall': sg.get_hall()}
            d['last_updated'] = datetime.datetime.today()
            return d
        except Exception as ex:
            logger.error("Error in " + os.path.abspath(dirname) +
                         ".\nError msg: " + str(ex))
            return None

    def get_valid_paths(self, path):
        (parent, subdirs, files) = path
        if 'relax1' in subdirs:
            return [parent]
        if ((not parent.endswith(os.sep + 'relax1')) and
                (not parent.endswith(os.sep + 'relax2')) and
                len(glob.glob(os.path.join(parent, "vasprun.xml*"))) > 0):
            return [parent]
        return []

    def convert(self, d):
        return d

    def __str__(self):
        return "VaspToDbDictDrone"

    @property
    def to_dict(self):
        init_args = {'additional_fields': self._additional_fields}
        output = {'name': self.__class__.__name__,
                  'init_args': init_args, 'version': __version__}
        return output


def get_basic_analysis_and_error_checks(d):
    initial_vol = d['input']['crystal']['lattice']['volume']
    final_vol = d['output']['crystal']['lattice']['volume']
    delta_vol = final_vol - initial_vol
    percent_delta_vol = delta_vol / initial_vol
    coord_num = get_coordination_numbers(d)
    gap = d['calculations'][-1]['output']['bandgap']
    cbm = d['calculations'][-1]['output']['cbm']
    vbm = d['calculations'][-1]['output']['vbm']
    is_direct = d['calculations'][-1]['output']['is_gap_direct']

    if abs(percent_delta_vol) > 0.20:
        warning_msgs = ['Volume change > 20%']
    else:
        warning_msgs = []

    bv_struct = Structure.from_dict(d["output"]["crystal"])
    try:
        bva = BVAnalyzer()
        bv_struct = bva.get_oxi_state_decorated_structure(bv_struct)
    except ValueError as e:
        logger.error("Valence cannot be determined due to {e}."
                     .format(e=e))
    except Exception as ex:
        logger.error("BVAnalyzer error {e}.".format(e=str(ex)))

    return {'delta_volume': delta_vol,
            'percent_delta_volume': percent_delta_vol,
            'warnings': warning_msgs, 'coordination_numbers': coord_num,
            'bandgap': gap, 'cbm': cbm, 'vbm': vbm,
            'is_gap_direct': is_direct,
            "bv_structure": bv_struct.to_dict}


def contains_vasp_input(dirname):
    for f in ['INCAR', 'POSCAR', 'POTCAR', 'KPOINTS']:
        if not os.path.exists(os.path.join(dirname, f)) and \
                not os.path.exists(os.path.join(dirname, f + ".orig")):
            return False
    return True


def get_coordination_numbers(d):
    structure = Structure.from_dict(d['output']['crystal'])
    f = VoronoiCoordFinder(structure)
    cn = []
    for i, s in enumerate(structure.sites):
        try:
            n = f.get_coordination_number(i)
            number = int(round(n))
            cn.append({'site': s.to_dict, 'coordination': number})
        except Exception:
            logger.error("Unable to parse coordination errors")
    return cn


def get_uri(mydir):
    fullpath = os.path.abspath(mydir)
    try:
        hostname = socket.gethostbyaddr(socket.gethostname())[0]
    except:
        hostname = socket.gethostname()
    return hostname + ":" + fullpath