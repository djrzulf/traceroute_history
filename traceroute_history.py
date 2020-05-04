#! /usr/bin/env python3
#  -*- coding: utf-8 -*-

"""
traceroute_history is a quick tool to make traceroute / tracert calls, and store it's results into a database if it
differs from last call.

Hosts can be configured in a config file,
Also happens to read smokeping configuration files to populate hosts to probe

"""

__intname__ = 'traceroute_history'
__author__ = 'Orsiris de Jong'
__copyright__ = 'Copyright (C) 2020 Orsiris de Jong'
__licence__ = 'BSD 3 Clause'
__version__ = '0.2.0'
__build__ = '2020050401'

import os
import sys
import re
import getopt
import ofunctions
from apscheduler.schedulers.background import BackgroundScheduler
from time import sleep
from sqlalchemy import create_engine, and_
from sqlalchemy.orm import sessionmaker, scoped_session
import sqlalchemy.exc
from datetime import datetime, timedelta
from command_runner import command_runner
import trparse
from sql_declaration import Target, Traceroute, Group, init_db
import configparser
from contextlib import contextmanager
from pprint import pprint
import urllib.parse

# colorama is not mandatory
try:
    import colorama
    if os.name == 'nt':
        colorama.init(convert=True)
except ImportError:
    pass

CONFIG_FILE = 'traceroute_history.conf'
SMOKEPING_CONFIG_FILE = None
DB_SESSION_FACTORY = None
DB_GLOBAL_READ_SESSION = None
CONFIG = None

LOG_FILE = os.path.join(os.path.dirname(__file__), os.path.splitext(os.path.basename(__file__))[0]) + '.log'
logger = ofunctions.logger_get_logger(log_file=LOG_FILE)


def diff_traceroutes(tr1: str, tr2: str):
    """
    Checks whether two traceroute outputs are different, and returns list of different hops

    :param tr1: (str) raw traceeroute output
    :param tr2: (str) raw
    :return: (list)
    """
    tr1_object = trparse.loads(tr1)
    tr2_object = trparse.loads(tr2)

    max_hops = max(len(tr1_object.hops), len(tr2_object.hops))

    different_hops = []

    # Traceroute indexes begin with 1 instead of 0
    for index in range(max_hops):
        try:
            if not tr1_object.hops[index].probes[0].ip == tr2_object.hops[index].probes[0].ip:
                different_hops.append(index)
        except IndexError:
            different_hops.append(index)

    return different_hops


def traceroutes_difference_formatted(tr1, tr2, formatting='console'):
    """
    Outputs traceroute differences with color highlighting in console
    :param tr1: (str) Traceroute sqlalchemy object
    :param tr2: (str) Tracerouet sqlalchemy object
    :return: (str) diff colorred traceroute outputs
    """
    different_hops = diff_traceroutes(tr1.traceroute, tr2.traceroute)

    if formatting == 'web':
        green_color = '<span class="traceroute-green" style="background-color: darkgreen; color:white">'
        red_color = '<span class="traceroute-red" style="background-color: darkred; color:white">'
        end_color = '</span>'
    else:
        try:
            green_color = colorama.Back.LIGHTGREEN_EX + colorama.Fore.BLACK
            red_color = colorama.Back.LIGHTRED_EX + colorama.Fore.BLACK
        except NameError:
            # missing colorama module ?
            green_color = '\033[102m'
            red_color = '\033[101m'
        end_color = '\033[0m'

    def _console_output(tr, color):
        console_output = ''
        for line in tr.split('\n'):
            # Check that line is a hop, also, hops
            try:
                # Since we count hop indexes from 0 in trparse, and hop output starts with 1
                index = int(line.split()[0]) - 1
            except (TypeError, IndexError, ValueError):
                console_output = '{0}{1}\n'.format(console_output, line)
                continue
            if index in different_hops:
                console_output = '{0}{1}{2}{3}\n'.format(console_output, color, line, end_color)
            else:
                console_output = '{0}{1}\n'.format(console_output, line)
        return console_output

    return 'Traceroute recorded at {0}:\n{1}Traceroute recorded at {2}:\n{3}'.format(tr1.creation_date,
                                                                                     _console_output(tr1.traceroute,
                                                                                                     green_color),
                                                                                     tr2.creation_date,
                                                                                     _console_output(tr2.traceroute,
                                                                                                     red_color))


def get_traceroute(address):
    """
    Launches actuel traceroute binary

    :param address: (str) address
    :return: (str) raw traceroute output
    """
    if address:
        if os.name == 'nt':
            executable = 'tracert'
            # Ugly hack se we get actual characters encoding right from cmd.exe
            # Also encodes "well" cp850 using cp437 parameter
            encoding = 'cp437'
        else:
            executable = 'traceroute'
            encoding = 'utf-8'
        command = '{0} {1}'.format(executable, address)
        exit_code, output = command_runner(command, shell=True, encoding=encoding)
        if exit_code == 0:
            return output
        else:
            logger.error(
                'Traceroute to address: "{0}" failed with exit code {1}. Command output:'.format(address, exit_code))
            logger.error(output)
    return None


@contextmanager
def db_scoped_session():
    """Provide a transactional scope around a series of operations."""
    session = DB_SESSION_FACTORY()
    try:
        yield session
        session.commit()
        session.flush()
    except:
        session.rollback()
        raise
    finally:
        session.close()


def insert_traceroute(target, traceroute_output):
    """
    Creates new traceroute entry in DB

    :param target: Target SQL object
    :param traceroute_output: raw traceroute output
    :return:
    """
    traceroute = Traceroute(traceroute=traceroute_output, target=target)
    with db_scoped_session() as session:
        session.add(traceroute)


def create_group(name, target):
    pass


def create_target(name, address=None, groups=None):
    """
    Creates new target (host) to monitor in DB

    :param name: (str) host user friendly name (can be anything)
    :param address: (str) hostname in fqdn, ipv4 or ipv6 format
    :param groups: (list)(str) list of groups to which this target belongs
    :return: (Target) target object
    """
    # for group in groups:
    #    try:
    #        group = db_session.query(Group).filter(Group.name == group).one()

    target = Target(name=name, address=address)
    with db_scoped_session() as session:
        session.add(target)

    return target


def update_traceroute_database(name, address, groups):
    """
    Executes tracert for given name, and updates database accordingly

    :param name: (str) host user friendly name (can be anything)
    :param address: (str) hostname in fqdn, ipv4 or ipv6 format
    :param groups: (list)(str) list of groups to which this target belongs
    :return:
    """

    try:
        with db_scoped_session() as session:
            try:
                target = session.query(Target).filter(Target.name == name).one()
            except sqlalchemy.orm.exc.NoResultFound:
                target = None
            if not target:
                # miss ipv4 and others
                target = create_target(name, address, groups)
                logger.info('Created new target: {0}.'.format(name))
            current_trace = get_traceroute(target.address)
            if current_trace:
                last_trace = session.query(Traceroute).filter(Traceroute.target == target).order_by(
                    Traceroute.id.desc()).first()
                if last_trace:
                    if diff_traceroutes(last_trace.traceroute, current_trace):
                        insert_traceroute(target, current_trace)
                        logger.info('Updating different traceroute for target: {0}.'.format(name))
                    else:
                        logger.debug('Traceroute identical to last one for target: {0}. Nothing to do.'.format(name))
                else:
                    insert_traceroute(target, current_trace)
                    logger.info('Created first tracreoute entry for target: {0}.'.format(name))
            else:
                logger.error('Cannot get traceroute for target: {0}.'.format(name))
    except sqlalchemy.exc.OperationalError as exc:
        logger.error('sqlalchemy operation error: {0}.'.format(exc))
        logger.error('Trace:', exc_info=True)


def get_last_traceroutes(name, limit=1):
    """
    Lists traceroute executions for a given target

    :param name: (str) target name
    :param limit: (int) number of executions to fetch, if None, all are fetched
    :return: (list)(Traceroutes) list of traceroute object

    """
    # Let's use a single global read session which we won't close so ORM objects are still mapped to session after usage
    session = DB_GLOBAL_READ_SESSION
    try:
        target = session.query(Target).filter(Target.name == name).one()
    except sqlalchemy.orm.exc.NoResultFound:
        return False

    last_trace = session.query(Traceroute).filter(Traceroute.target == target).order_by(Traceroute.id.desc()).limit(
        limit).all()
    return last_trace


def get_last_traceroutes_formatted(name, limit=1, formatting='console'):
    traceroutes = get_last_traceroutes(name, limit=limit)
    if traceroutes is False:
        logger.warning('Target {0} has been requested but does not exist in database.'.format(name))
        return 'Target not found in database.'
    if traceroutes:
        output = 'Target has {0} tracreoute entries.'.format(len(traceroutes))
        length = len(traceroutes)
        if len(traceroutes) > 1:
            output = output + traceroutes_difference_formatted(traceroutes[0], traceroutes[1], formatting=formatting)
            for i in range(length - 2):
                output = output + traceroutes[i + 2].__repr__()
        else:
            for traceroute in traceroutes:
                output = output + traceroute.__repr__()
    else:
        output = traceroutes
    if formatting == 'web' and output is not None and output != []:
        output = output.replace('\n', '<br />')
    return output

def list_targets():
    with db_scoped_session() as session:
        output = []
        try:
            targets = session.query(Target).all()
            for target in targets:
                output.append({'name': target.name, 'address': target.address, 'groups': target.groups})
            return output
        except sqlalchemy.orm.exc.NoResultFound:
            return None


def delete_old_traceroutes(name: str, days: int, keep: int):
    """
    Deletes old traceroute data if days have passed, but always keep at least limit entries

    :param name: (str) target name
    :param days: (int) number of days after which a traceroute will be deleted
    :param keep: (int) number of traceroutes to keep regardless of the
    :return:
    """

    with db_scoped_session() as session:
        try:
            target = session.query(Target).filter(Target.name == name).one()
        except sqlalchemy.orm.exc.NoResultFound:
            return None

        num_records = session.query(Traceroute).filter(Traceroute.target == target).count()
        if num_records > keep:
            num_records_to_delete = num_records - keep
            # Subquery is needed because we cannot use delete() on a query with a limit
            subquery = session.query(Traceroute.id).filter(and_(Traceroute.target == target,
                                                                Traceroute.creation_date < (datetime.now() - timedelta(
                                                                    days=days)))).order_by(Traceroute.id.desc()).limit(
                num_records_to_delete).subquery()
            records = session.query(Traceroute).filter(Traceroute.id.in_(subquery)).delete(synchronize_session='fetch')
            logger.info('Deleted {0} old records for target: {1}.'.format(records, name))


def read_smokeping_config(config_file):
    """
    Read smokeping config file
    TODO: does not support missing title or host directives (will shift values)

    :param config_file: (str) path to config file
    :return: (list)(dict) [{'host': x, 'title': y}]
    """
    if config_file == '':
        return None
    if not os.path.isfile(config_file):
        logger.error('smokeping config "{0}" does not seem to be a file.'.format(config_file))
        return None

    host_regex = re.compile(r'^host\s*=\s*(\S*)$')
    title_regex = re.compile(r'^title\s*=\s*(.*)$')

    hosts = []
    names = []

    with open(config_file, 'r') as smokeping_config:
        for line in smokeping_config:
            host = re.match(host_regex, line)
            name = re.match(title_regex, line)
            if host:
                hosts.append(host)
            if name:
                names.append(host)

    if len(hosts) != len(names):
        logger.error('Cannot parse smokeping config file. We need as much titles as host entries.')
        return None

    # TODO Add regex for group inclusion / exclusion

    return [{'host': host, 'name': name} for host, name in zip(hosts, names)]


def execute(daemon=False):
    """
    Execute traceroute updates and housekeeping for all hosts

    :param daemon: (bool) Should this run in a loop
    :return:
    """
    config = CONFIG
    hosts = [section for section in config.sections() if section.startswith('HOST_')]
    try:
        smokeping_config = config['SMOKEPING_SOURCE']['smokeping_config_path']
    except KeyError:
        smokeping_config = None
    smokeping_hosts = read_smokeping_config(smokeping_config)
    if smokeping_hosts:
        hosts = hosts + smokeping_hosts

    if len(hosts) == 0:
        logger.info('No valid hosts given.')
        sys.exit(20)

    scheduler = BackgroundScheduler()
    scheduler.start()

    # Interval between traceroute executions
    try:
        interval = int(config['TRACEROUTE_HISTORY']['interval'])
    except KeyError:
        interval = 3600
    except TypeError:
        logger.error('Bogus interval value. Using default value.')
        interval = 3600

    try:
        delete_history_days = int(config['TRACEROUTE_HISTORY']['delete_history_days'])
    except KeyError:
        delete_history_days = None
    except TypeError:
        logger.error('Bogus delete_history_days value. Deactivating cleanup.')
        delete_history_days = None
    try:
        minimum_keep = int(config['TRACEROUTE_HISTORY']['minimum_keep'])
    except KeyError:
        minimum_keep = 100
    except TypeError:
        logger.error('Bogus minimum_keep value. Using default.')
        minimum_keep = 100

    for host in hosts:
        try:
            target_name = config[host]['name']
            job_kwargs = {
                'name': config[host]['name'],
                'address': config[host]['address'],
                'groups': config[host]['groups']
            }
            delete_kwargs = {
                'name': config[host]['name'],
                'days': delete_history_days,
                'keep': minimum_keep
            }

            # Immediate start
            scheduler.add_job(update_traceroute_database, None, [], job_kwargs, name='startup-' + target_name,
                              id='startup-' + target_name)
            # Programmed start afterwards
            scheduler.add_job(update_traceroute_database, 'interval', [], job_kwargs, seconds=interval,
                              name=target_name, id=target_name)

            if delete_history_days:
                scheduler.add_job(delete_old_traceroutes, None, [], delete_kwargs,
                                  name='startup-housekeeping-' + target_name, id='startup-housekeeping-' + target_name)
                scheduler.add_job(delete_old_traceroutes, 'interval', [], delete_kwargs, hours=1,
                                  name='housekeeping-' + target_name, id='housekeeping-' + target_name)
        except KeyError as exc:
            logger.error('Failed to read configuration for host: {0}: {1}.'.format(host, exc))

    run_once = True
    try:
        while daemon or run_once:
            run_once = False
            sleep(1)
    except KeyboardInterrupt:
        logger.info('Interrupted by keyboard')
        scheduler.shutdown()


def load_config():
    """
    Loads config from file

    :return: (ConfigParser) config object
    """
    if CONFIG_FILE is None or not os.path.isfile(CONFIG_FILE):
        print(
            'Cannot load configuration file: {0}. Please use --config=[config file].'.format(CONFIG_FILE))
        sys.exit(10)
    config = configparser.ConfigParser()
    try:
        config.read(CONFIG_FILE)
    except (configparser.MissingSectionHeaderError, KeyError):
        print('Unknown database configuration.')
        sys.exit(12)
    return config


def load_database(db_driver=None, db_host=None, db_user=None, db_password=None, db_name=None, initialize=False):
    """
    Initiates database session as scoped session so we can reutilise the factory in a threaded model

    :return:
    """
    global DB_SESSION_FACTORY
    global DB_GLOBAL_READ_SESSION

    if db_driver == 'sqlite' and not os.path.isfile(db_host) and initialize is False:
        logger.critical('No database file: "{0}". Please provide path in configuration file, or use --init-db to create a new database.'.format(db_host))
        sys.exit(3)

    if db_driver == 'sqlite':
        db_name = ''
    elif db_name:
        db_name = '/' + db_name

    if db_user and db_password and db_driver != 'sqlite':
        connection_string = '{0}:///{1}:{2}@{3}{4}'.format(db_driver, db_user, db_password, db_host, db_name)
    else:
        connection_string = '{0}:///{1}{2}'.format(db_driver, db_host, db_name)

    print(connection_string)
    if initialize:
        db_engine = create_engine(connection_string, echo=True)
        init_db(db_engine)
        logger.info('DB engine initialization finished.')
        sys.exit(0)
    else:
        try:
            logger.info('Trying to open {0} database "{1}{2}" as user "{2}".'.format(db_driver, db_host, db_name, db_user, db_password))
            engine = create_engine(connection_string, echo=False)
            session_factory = sessionmaker(bind=engine)
            DB_SESSION_FACTORY = scoped_session(session_factory)
            DB_GLOBAL_READ_SESSION = DB_SESSION_FACTORY()
        except sqlalchemy.exc.OperationalError:
            logger.critical('Cannot connect to database "{0}".'.format(db_host), exc_info=True)


def help_():
    print('{} {} {}'.format(__intname__, __version__, __build__))
    print('{} under {}'.format(__copyright__, __licence__))
    print('')
    print('Usage:')
    print('{} [options]'.format(__file__))
    print('')
    print('Options:')
    print('')
    print(
        '--config=                            Path to config file. If none given, traceroute_history.conf in the current directory is tried.')
    print(
        '--smokeping-config=                  Optional path to smokeping config, in order to read additional targets from')
    print('--daemon                             Run as daemon')
    print('--update-now                         Manual update of traceroute targets')
    print(
        '--get-traceroutes-for=host[,x]       Print x traceroutes for target "host". If no x value is given, all are shown')
    print('--list-targets                       Extract a list of current targets in database"')
    print('--init-db                            Initialize a fresh database.')
    sys.exit()


def main(argv):
    global CONFIG
    global CONFIG_FILE
    global SMOKEPING_CONFIG_FILE
    global logger

    try:
        opts, _ = getopt.getopt(argv, "h?",
                                ['config=', 'smokeping-config=', 'get-traceroutes-for=', 'list-targets',
                                 'daemon', 'update-now',
                                 'init-db', 'help'])
    except getopt.GetoptError:
        help_()
        sys.exit(9)

    config_file_set = False
    for opt, arg in opts:
        if opt == '--config':
            CONFIG_FILE = arg
            config_file_set = True
        if opt == '--smokeping-config':
            SMOKEPING_CONFIG_FILE = arg

    # Reload config before executing anything elsee
    CONFIG = load_config()
    try:
        log_file = CONFIG['TRACEROUTE_HISTORY']['log_file']
        logger = ofunctions.logger_get_logger(log_file=log_file)
    except KeyError:
        pass

    if not config_file_set:
        logger.info('No config file set. trying default one: {0}.'.format(os.path.abspath(CONFIG_FILE)))

    if os.name != 'nt':
        if os.getuid() != 0:
            logger.warn('This program should probably be run as root so traceroute can work.')

    try:
        db_user = urllib.parse.quote_plus(CONFIG['TRACEROUTE_HISTORY']['database_user'])
        db_password = urllib.parse.quote_plus(CONFIG['TRACEROUTE_HISTORY']['database_password'])
    except KeyError:
        db_user = None
        db_password = None
    try:
        db_name = CONFIG['TRACEROUTE_HISTORY']['database_name']
    except KeyError:
        db_name = None

    initialize = False
    for opt, arg in opts:
        if opt == '--init-db':
            initialize = True

    load_database(db_driver=CONFIG['TRACEROUTE_HISTORY']['database_driver'],
                  db_host=CONFIG['TRACEROUTE_HISTORY']['database_host'],
                  db_name=db_name,
                  db_user=db_user,
                  db_password=db_password,
                  initialize=initialize)

    opt_found = False
    for opt, arg in opts:
        if opt == '--get-traceroutes-for':
            opt_found = True
            try:
                host, limit = arg.split(',')
                limit = int(limit)
            except (ValueError, TypeError):
                host = arg
                limit = None
            print(get_last_traceroutes_formatted(host, limit))
            sys.exit(0)
        if opt == '--list-targets':
            opt_found = True
            pprint(list_targets())
            sys.exit(0)
        if opt == '--daemon':
            opt_found = True
            execute(daemon=True)
        if opt == '--update-now':
            opt_found = True
            execute()
        if opt == '--help' or opt == 'h' or opt == '?':
            opt_found = True
            help_()
    if not opt_found:
        help_()


if __name__ == '__main__':
    main(sys.argv[1:])
