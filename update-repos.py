#!/usr/bin/env python2.7
#  vim:se fileencoding=utf8
# (c) 2015 Michał Górny
# note: 2.7 needed because of pkgcore, awesome

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

try:
    import urllib.error
    import urllib.request
except ImportError:
    import urllib2
    class urllib:
        error = urllib2
        request = urllib2

import datetime
import email.utils
import json
import os
import os.path
import pickle
import pprint
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree

try:
    DEVNULL = subprocess.DEVNULL
except AttributeError:
    DEVNULL = open('/dev/null', 'r')

class State(object):
    # removed (no longer exists remotely)
    REMOVED = 'REMOVED'
    # unsupported repo type
    UNSUPPORTED = 'UNSUPPORTED'
    # unable to sync
    SYNC_FAIL = 'SYNC_FAIL'
    # empty repository (no files at all)
    EMPTY = 'EMPTY'
    # missing repo_name
    MISSING_REPO_NAME = 'MISSING_REPO_NAME'
    # conflicting repo_name
    CONFLICTING_REPO_NAME = 'CONFLICTING_REPO_NAME'
    # missing masters
    MISSING_MASTERS = 'MISSING_MASTERS'
    # invalid masters
    INVALID_MASTERS = 'INVALID_MASTERS'
    # invalid metadata (?)
    INVALID_METADATA = 'INVALID_METADATA'
    # bad cache
    BAD_CACHE = 'BAD_CACHE'
    # good
    GOOD = 'GOOD'


class SkipRepo(Exception):
    pass


class SourceMapping(object):
    """ Map layman repository info to repos.conf """

    def git(self, uri, branch):
        if branch:
            raise SkipRepo('Branches are not supported')
        return {
            'sync-type': 'git',
            'sync-depth': '0',
            'sync-uri': uri,
            'x-vcs-preference': 0,
        }

    def mercurial(self, uri, branch):
        if branch:
            raise SkipRepo('Branches are not supported')
        return {
            'sync-type': 'hg',
            'sync-uri': uri,
            'x-vcs-preference': 5,
        }

    def rsync(self, uri, branch):
        if branch:
            raise SkipRepo('Branches in rsync, wtf?!')
        return {
            'sync-type': 'rsync',
            'sync-uri': uri,
            'x-vcs-preference': 100,
        }

    def svn(self, uri, branch):
        if branch:
            raise SkipRepo('Svn branches not supported')
        return {
            'sync-type': 'git',
            'sync-uri': uri if uri.startswith('svn://') else 'svn+' + uri,
            'x-vcs-preference': 10,
        }

    def bzr(self, uri, branch):
        if branch:
            raise SkipRepo('Svn branches not supported')
        return {
            'sync-type': 'bzr',
            'sync-uri': uri,
            'x-vcs-preference': 5,
        }


class LoggerProxy(object):
    def __init__(self, logdir, key):
        self._path = os.path.join(logdir, key + '.txt')
        self._key = key

    def status(self, msg):
        sys.stderr.write('[%s] %s\n' % (self._key, msg))
        with open(self._path, 'a') as f:
            f.write(' * %s\n' % msg)

    def command(self, cmd):
        with open(self._path, 'a') as f:
            f.write('$ %s\n' % ' '.join(cmd))

    def open(self):
        return open(self._path, 'a')


class Logger(object):
    def __init__(self, log_root):
        dt = datetime.datetime.utcnow()
        self.log_dir = os.path.join(log_root, dt.strftime('%Y-%m-%dT%H:%M:%S'))
        os.makedirs(self.log_dir)

    def __getitem__(self, key):
        return LoggerProxy(self.log_dir, key)

    def write_summary(self, data):
        with open(os.path.join(self.log_dir, 'summary.json'), 'w') as f:
            json.dump(data, f)


class LazySubprocess(object):
    def __init__(self, log, *args, **kwargs):
        self._log = log
        self._args = args
        self._kwargs = kwargs
        self._s = None
        self._running = False

    def start(self):
        kwargs = self._kwargs
        self._log.command(self._args[0])
        with self._log.open() as f:
            kwargs['stdin'] = DEVNULL
            kwargs['stdout'] = f
            kwargs['stderr'] = subprocess.STDOUT
            self._s = subprocess.Popen(*self._args, **kwargs)
        self._running = True

    @property
    def running(self):
        return self._running

    def poll(self):
        assert(self._running)
        return self._s.poll()


class TaskManager(object):
    def __init__(self, max_jobs, log):
        self._max_jobs = max_jobs
        self._jobs = {}
        self._queue = []
        self._results = {}
        self._log = log

    def add(self, name, *args, **kwargs):
        subp = LazySubprocess(self._log[name], *args, **kwargs)
        if len(self._jobs) < self._max_jobs:
            subp.start()
            self._jobs[name] = subp
        else:
            self._queue.append((name, subp))

    def wait(self):
        while self._jobs or self._queue:
            to_del = []
            for n, s in self._jobs.items():
                ret = s.poll()
                if ret is not None:
                    self._results[n] = ret
                    yield (n, ret)
                    to_del.append(n)
            for n in to_del:
                del self._jobs[n]

            while len(self._jobs) < self._max_jobs and self._queue:
                n, s = self._queue.pop(0)
                s.start()
                self._jobs[n] = s
            
            time.sleep(0.25)

    def get_result(self, r):
        return self._results[r]


def main():
    REPOSITORIES_XML = os.environ['REPOSITORIES_XML']
    REPOSITORIES_XML_CACHE = os.environ['REPOSITORIES_XML_CACHE']

    CONFIG_ROOT = os.environ['CONFIG_ROOT']
    CONFIG_ROOT_SYNC = os.environ['CONFIG_ROOT_SYNC']
    LOG_DIR = os.environ['LOG_DIR']
    SYNC_DIR = os.environ['SYNC_DIR']
    REPOS_DIR = os.environ['REPOS_DIR']
    REPOS_CONF = os.environ['REPOS_CONF']

    MAX_SYNC_JOBS = int(os.environ['MAX_SYNC_JOBS'])
    MAX_REGEN_JOBS = int(os.environ['MAX_REGEN_JOBS'])
    MAX_PCHECK_JOBS = int(os.environ['MAX_PCHECK_JOBS'])
    REGEN_THREADS = os.environ['REGEN_THREADS']

    BANNED_REPOS = frozenset(os.environ['BANNED_REPOS'].split())

    log = Logger(LOG_DIR)
    states = {}

    os.environ['PORTAGE_CONFIGROOT'] = CONFIG_ROOT_SYNC

    # collect all local and remote repositories
    sys.stderr.write('* updating repository list\n')
    req = urllib.request.Request(REPOSITORIES_XML)
    req.add_header('User-Agent', 'repo-mirror-ci')

    try:
        req.add_header('If-Modified-Since',
                email.utils.formatdate(
                    os.stat(REPOSITORIES_XML_CACHE).st_mtime))
    except OSError:
        pass

    try:
        f = urllib.request.urlopen(req)
        try:
            with open(REPOSITORIES_XML_CACHE, 'wb') as outf:
                outf.write(f.read())
                ts = time.mktime(email.utils.parsedate(f.info()['Last-Modified']))
            # py2 can't do fd here...
            os.utime(REPOSITORIES_XML_CACHE, (ts, ts))
        finally:
            f.close()
    except urllib.error.HTTPError as e:
        if e.code == 304:
            print('- note: file up-to-date')
        else:
            print('!!! Warning: fetch failed: %s' % e)

    repos_xml = xml.etree.ElementTree.parse(REPOSITORIES_XML_CACHE).getroot()

    remote_repos = frozenset(
            r.find('name').text for r in repos_xml)
    remote_repos = remote_repos.difference(BANNED_REPOS)

    # collect local repository configuration
    sys.stderr.write('* updating repos.conf\n')
    repos_conf = configparser.ConfigParser()
    repos_conf.read([os.path.join(CONFIG_ROOT_SYNC, REPOS_CONF)])
    local_repos = frozenset(repos_conf.sections())

    # 1. remove repos that no longer exist
    to_remove = list(local_repos.difference(remote_repos))
    for r in sorted(to_remove):
        states[r] = {'x-state': State.REMOVED}
        log[r].status('Removing, no longer on remote list')
        repos_conf.remove_section(r)

    # 2. update URIs for local repos, add new repos
    srcmap = SourceMapping()
    existing_repos = []
    for repo_el in sorted(repos_xml, key=lambda r: r.find('name').text):
        r = repo_el.find('name').text

        # construct data out of mixture of attributes and elements
        data = {}
        data.update(repo_el.items())
        for el in repo_el:
            if el.tag in ('description', 'longdescription'):
                # multi-lingua
                if el.tag not in data:
                    data[el.tag] = {}
                data[el.tag][el.get('lang', 'en')] = el.text
            elif el.tag in ('owner', 'source', 'feed'):
                # possibly multiple

                if el.tag == 'owner':
                    # nested
                    val = {}
                    val.update(el.items())
                    val.update((x.tag, x.text) for x in el)
                elif el.tag == 'source':
                    # attributed
                    val = {}
                    val.update(el.items())
                    val['uri'] = el.text
                else:
                    val = el.text

                if el.tag not in data:
                    data[el.tag] = []
                data[el.tag].append(val)
            else:
                data[el.tag] = el.text

        states[r] = data
        with log[r].open() as f:
            pprint.pprint(states[r], f)

        possible_configs = []
        for s in data['source']:
            try:
                possible_configs.append(
                        getattr(srcmap, s['type'])(s['uri'], None))
            except SkipRepo as e:
                log[r].status('Skipping %s: %s' % (s['uri'], str(e)))

        if not possible_configs:
            states[r]['x-state'] = State.UNSUPPORTED
            if repos_conf.has_section(r):
                repos_conf.remove_section(r)
                to_remove.append(r)
            continue

        # choose the first URI for most preferred protocol (stable sort)
        vals = sorted(possible_configs, key=lambda x: x['x-vcs-preference'])[0]
        del vals['x-vcs-preference']

        if not repos_conf.has_section(r):
            log[r].status('Adding new repository')
            repos_conf.add_section(r)
        repo_path = os.path.join(SYNC_DIR, r)
        repos_conf.set(r, 'location', repo_path)

        if os.path.exists(repo_path):
            # check whether sync params changed
            for k, v in vals.items():
                if not repos_conf.has_option(r, k) or repos_conf.get(r, k) != v:
                    log[r].status('Resetting, sync parameters changed')
                    to_remove.append(r)
                    break
            else:
                existing_repos.append(r)

        for k, v in vals.items():
            repos_conf.set(r, k, v)

    # 3. write new repos.conf, remove stale checkouts
    with open(os.path.join(CONFIG_ROOT_SYNC, REPOS_CONF), 'w') as f:
        repos_conf.write(f)
    for r in to_remove:
        if os.path.exists(os.path.join(SYNC_DIR, r)):
            shutil.rmtree(os.path.join(SYNC_DIR, r))
        if os.path.exists(os.path.join(REPOS_DIR, r)):
            shutil.rmtree(os.path.join(REPOS_DIR, r))
    local_repos = frozenset(repos_conf.sections())

    # 4. sync all repos
    sys.stderr.write('* syncing repositories\n')
    sync_start = datetime.datetime.utcnow()
    jobs = []
    syncman = TaskManager(MAX_SYNC_JOBS, log)
    for r in sorted(local_repos):
        syncman.add(r, ['pmaint', 'sync', r])

    # 5. check for sync failures
    to_readd = []
    for r, st in syncman.wait():
        if st == 0:
            log[r].status('Sync succeeded')
            states[r]['x-state'] = State.GOOD
        else:
            log[r].status('Sync failed with %d' % st)
            if r in existing_repos:
                log[r].status('Will try to re-create')
                if os.path.exists(os.path.join(SYNC_DIR, r)):
                    shutil.rmtree(os.path.join(SYNC_DIR, r))
                to_readd.append(r)
            else:
                states[r]['x-state'] = State.SYNC_FAIL
                repos_conf.remove_section(r)

    sync_finish = datetime.datetime.utcnow()
    sys.stderr.write('** total syncing time: %s\n' % (sync_finish - sync_start))

    # 6. remove local checkouts and sync again
    for r in sorted(to_readd):
        syncman.add(r, ['pmaint', 'sync', r])

    for r, st in syncman.wait():
        if st == 0:
            log[r].status('Sync succeeded after re-adding')
            states[r]['x-state'] = State.GOOD
        else:
            log[r].status('Sync failed again with %d, removing' % st)
            states[r]['x-state'] = State.SYNC_FAIL
            if os.path.exists(os.path.join(SYNC_DIR, r)):
                shutil.rmtree(os.path.join(SYNC_DIR, r))
            if os.path.exists(os.path.join(REPOS_DIR, r)):
                shutil.rmtree(os.path.join(REPOS_DIR, r))
            repos_conf.remove_section(r)

    with open(os.path.join(CONFIG_ROOT_SYNC, REPOS_CONF), 'w') as f:
        repos_conf.write(f)
    local_repos = frozenset(repos_conf.sections())

    # 7. check all added repos for invalid metadata:
    # - correct & matching repo_name (otherwise mischief will happen)
    # - correct masters= (otherwise pkgcore will fail)
    # - possibly other stuff causing pkgcore to fail hard
    # TODO: gracefully skip repos when masters failed to sync

    import pkgcore.config

    pkgcore_config = pkgcore.config.load_config()
    for r in sorted(local_repos):
        config_sect = pkgcore_config.collapse_named_section(r)
        repo_config = config_sect.config['repo_config'].instantiate()

        p_repo_id = repo_config.repo_id
        p_masters = repo_config.masters
        if repo_config.is_empty:
            log[r].status('Empty repository, removing')
            states[r]['x-state'] = State.EMPTY
        elif not p_repo_id:
            log[r].status('Missing repo_name, removing')
            states[r]['x-state'] = State.MISSING_REPO_NAME
        elif p_repo_id != r:
            log[r].status('Conflicting repo_name, removing ("%s" in repo_name, "%s" on list)' % (p_repo_id, r))
            states[r]['x-state'] = State.CONFLICTING_REPO_NAME
            states[r]['x-repo-name'] = p_repo_id
        elif p_masters is None:
            log[r].status('Missing masters!')
            states[r]['x-state'] = State.MISSING_MASTERS
        else:
            for m in p_masters:
                if m not in remote_repos:
                    log[r].status('Invalid/unavailable master = %s, removing' % m)
                    states[r]['x-state'] = State.INVALID_MASTERS

        if states[r]['x-state'] in (State.GOOD,):
            # we check this since failure to instantiate a repo will
            # prevent pkgcore from operating further
            try:
                pkgcore_repo = config_sect.instantiate()
            except Exception as e:
                log[r].status('Invalid metadata, removing: %s' % str(e))
                if states[r]['x-state'] == State.GOOD:
                    states[r]['x-state'] = State.INVALID_METADATA

        if states[r]['x-state'] not in (State.GOOD,):
            repos_conf.remove_section(r)

    # 8. moves repos from SYNC_DIR to REPOS_DIR
    s = subprocess.Popen(['rsync', '-rlpt', '--delete', '--exclude=.*/',
        '--exclude=*/metadata/md5-cache', '--exclude=*/profiles/use.local.desc',
        '--exclude=*/metadata/pkg_desc_index', '--exclude=*/metadata/timestamp.chk',
        os.path.join(SYNC_DIR, '.'), REPOS_DIR])
    s.wait()
    local_repos = frozenset(repos_conf.sections())
    for r in local_repos:
        repos_conf.set(r, 'location', os.path.join(REPOS_DIR, r))
    with open(os.path.join(CONFIG_ROOT, REPOS_CONF), 'w') as f:
        repos_conf.write(f)
    os.environ['PORTAGE_CONFIGROOT'] = CONFIG_ROOT
    #pkgcore_config = pkgcore.config.load_config()

    # 9. regen caches for all repos
    sys.stderr.write('* regenerating cache\n')
    regen_start = datetime.datetime.utcnow()
    regenman = TaskManager(MAX_REGEN_JOBS, log)
    for r in sorted(local_repos):
        regenman.add(r, ['pmaint', 'regen',
            '--use-local-desc', '--pkg-desc-index',
            '-t', REGEN_THREADS, r])

    for r, st in regenman.wait():
        if st == 0:
            log[r].status('Cache regenerated successfully')
        else:
            log[r].status('Cache regen failed with %d' % st)
            # don't override higher priority issues here
            if states[r]['x-state'] == State.GOOD:
                states[r]['x-state'] = State.BAD_CACHE

    regen_finish = datetime.datetime.utcnow()
    sys.stderr.write('** total regen time: %s\n' % (regen_finish - regen_start))

    log.write_summary(states)

    # 10. run pkgcheck
    # disabled because pkgcheck does not support masters currently
    if False:
        pkgcheckman = TaskManager(MAX_PCHECK_JOBS, log)
        for r in sorted(local_repos):
            pkgcheckman.add(r, ['pkgcheck', '-r', repos_conf.get(r, 'location'),
                '--reporter=FancyReporter',
                '--color=yes',
                '--profile-disable-dev',
                '--profile-disable-deprecated',
                '--profile-disable-exp',
                '*/*'])

        for r, st in pkgcheckman.wait():
            if st == 0:
                log[r].status('pkgcheck ran successfully')
            else:
                # shouldn't happen really
                log[r].status('pkgcheck failed with %d' % st)


if __name__ == '__main__':
    main()
