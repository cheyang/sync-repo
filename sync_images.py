#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import docker
import dateutil.parser

import logging
import sys
import getopt
import requests
import datetime
import time
import re
import subprocess
import json
import threading
try:
    import queue
except:
    # py2 compat
    import Queue as queue



logging.getLogger().setLevel(logging.INFO)

tag_filters = ['git-.*', 'canary$', 'dev$', 'dev-.*', 'build-.*', '.*-dirty$']
tag_filters += ['^master-.*'] # zeppelin
tag_filters += ['.*-alpha.*', '.*-beta.*', '.*-rc.*']


def match_tag(tag):
    for filter in tag_filters:
        if re.match(filter, tag):
            return True
    return False


def help():
    print('python sync_images -h|--help')
    print('python sync_images [-f|--file <config_file>] [-r|--registry <host:port>] [-n|--namespace] [-i|--insecure_registry] [-d|--days 15] [-c|--recents 0]')
    sys.exit(1)


def normalize_repo(repo):
    repo_names = repo.split('/', 2)
    if len(repo_names) == 1:
        repo_names = ['docker.io', 'library', repo_names[0]]
    if len(repo_names) == 2:
        if '.' in repo_names[0]:
            repo_names = [repo_names[0], '', repo_names[1]] # Like k8s.gcr.io/kube-apiserver
        else:
            repo_names = ['docker.io', repo_names[0], repo_names[1]]
    return repo_names


def searchTags(url, key):
    r = requests.get(url)
    logging.info('Search repository %s from url %s ...' % (repo, url))
    if r.status_code == 200:
        return r.json().get(key, [])
    else:
        logging.info('Failed to list image tags with error code:%d message:%s' % (r.status_code, r.text))
        return {}

def run(cmd):
    return subprocess.check_output(cmd, shell=True)

def searchTagsWith(cmd, key):
    output = run(cmd)
    logging.info('Search repository %s with cmd %s ...' % (repo, cmd))
    return json.loads(output).get("data").get(key, [])

class Tag(object):
    def __init__(self, name, ts):
        self.name = name
        self.ts = ts

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, tag1):
        return self.name == tag1.name

def list_repo_tags(client, repo):
    result = []
    repo_names = normalize_repo(repo)
    timestamp = time.mktime((datetime.date.today() - datetime.timedelta(days=days)).timetuple()) * 1000
    tag_set = set()
    if repo_names[0] == 'docker.io':
        url = "https://registry.hub.docker.com/v2/repositories/%s/%s/tags/?page_size=1024" % (repo_names[1], repo_names[2])
        tags = searchTags(url, 'results')
        for image in tags:
            timeUpload = time.mktime(dateutil.parser.parse(image['last_updated']).timetuple())*1000
            tag = image['name']
            if len(tag) > 0:
                tag_set.add(Tag(tag, timeUpload))
    elif repo_names[0] == 'quay.io':
        url = 'https://quay.io/api/v1/repository/%s/%s/tag/' % (repo_names[1], repo_names[2])
        tags = searchTags(url, 'tags')
        for image in tags:
            timeUpload = float(image['start_ts']) * 1000
            tag = image['name']
            if len(tag) > 0:
                tag_set.add(Tag(tag, timeUpload))
    elif repo_names[0].endswith("aliyuncs.com"):
        # url = 'https://quay.io/api/v1/repository/%s/%s/tag/' % (repo_names[1], repo_names[2])
        # | jq '.data'
        endpoint = repo_names[0].split(".")[1]
        cmd = "aliyun cr GET  /repos/%s/%s/tags --endpoint cr.%s.aliyuncs.com"  % (repo_names[1], repo_names[2], endpoint)
        tags = searchTagsWith(cmd, 'tags')
        for image in tags:
            timeUpload = float(image['imageUpdate']) #* 1000
            tag = image['tag']
            # Only list the layer with tag
            if len(tag) > 0:
                tag_set.add(Tag(tag, timeUpload))
    else:
        if repo_names[1] == '':
            url = 'https://%s/v2/%s/tags/list' % (repo_names[0], repo_names[2])
        else:
            url = 'https://%s/v2/%s/%s/tags/list' % (repo_names[0], repo_names[1], repo_names[2])
        manifest = searchTags(url, u'manifest')
        for key in manifest:
            image = manifest[key]
            timeUpload = float(image[u'timeUploadedMs'])
            tags = image[u'tag']

            for tag in tags:
                # Ignore the canary and alpha images
                if len(tag) == 0 or match_tag(tag):
                    continue
                tag_set.add(Tag(tag, timeUpload))

    tag_list = sorted(tag_set, key=lambda t: t.ts, reverse=True)
    for tag in tag_list:
        if tag.ts > timestamp or len(result) < recents:
            result.append(tag.name)
    return result


def sync_repo(client, registry, namespace, insecure_registry, repo, newName):
    tags = list_repo_tags(client, repo)
    new_repo = registry + '/' + namespace + '/' + newName
    for tag in tags:
        queue_pull.put((repo, new_repo, tag))


options = []
DEFAULT_CONFIG_FILE = './images.txt'
DEFAULT_REGISTRY = 'registry.cn-hangzhou.aliyuncs.com'
DEFAULT_NAMESPACE = 'google_containers'
INSECURE_REGISTRY = False
DEFAULT_DAYS = 15
DEFAULT_RECENTS = 0

docker_host = None
insecure_registry = INSECURE_REGISTRY
filename = DEFAULT_CONFIG_FILE
days = DEFAULT_DAYS
recents = DEFAULT_RECENTS
# parse command line arguments

try:
    (options, args) = getopt.getopt(sys.argv[1:], 'f:d:c:r:n:ih', ['file=', 'days=', "recents=", 'registry=', 'namespace=', 'insecure_registry', 'help'])
except getopt.GetoptError:
    help()
namespace = DEFAULT_NAMESPACE
registry = DEFAULT_REGISTRY
for option in options:
    if option[0] == '-f' or option[0] == '--file':
        filename = option[1]
    elif option[0] == '-r' or option[0] == '--registry':
        registry = option[1]
    elif option[0] == '-n' or option[0] == '--namespace':
        namespace = option[1]
    elif option[0] == '-i' or option[0] == '--insecure_registry':
        insecure_registry = True
    elif option[0] == '-d' or option[0] == '--days':
        days = int(option[1])
    elif option[0] == '-c' or option[0] == '--recents':
        recents = int(option[1])
    elif option[0] == '-h' or option[0] == '--help':
        help()

try:
    with open(filename) as fin:
        lines = [line.strip() for line in fin.readlines()]
except Exception as ex:
    logging.error('Read configuration %s for image sync: %s' \
        % (filename, ex))
    sys.exit(1)

nt = 4
queue_pull = queue.Queue(maxsize=nt)
queue_push = queue.Queue(maxsize=nt*2)
def thread_pull():
    client = docker.from_env()
    while True:
        args = queue_pull.get()
        if args is None:
            return
        repo, new_repo, tag = args
        try:
            logging.info('Pulling %s:%s' % (repo, tag))
            image = client.images.pull(repo, tag=tag)

            logging.info('Tagging %s:%s' % (new_repo, tag))
            image.tag(new_repo, tag)

            queue_push.put(args)
        except Exception:
            logging.exception("when pulling/tagging %s" % (args, ))

def thread_push():
    client = docker.from_env()
    while True:
        args = queue_push.get()
        if args is None:
            return
        repo, new_repo, tag = args
        try:
            logging.info('Pushing %s:%s' % (new_repo, tag))
            client.images.push(new_repo, tag=tag)
            logging.info('Pushing done: %s:%s' % (new_repo, tag))
        except:
            logging.exception("when pushing %s" % (args, ))

pull_threads = [threading.Thread(target=thread_pull) for i in range(nt)]
for t in pull_threads:
    t.start()
push_threads = [threading.Thread(target=thread_push) for i in range(nt)]
for t in push_threads:
    t.start()


client = docker.from_env()
for line in lines:
    # Ignore comment
    if line.startswith('#'):
        continue
    if line == '':
        continue

    try:
        ns = namespace
        repos = line.split("=")
        if len(repos) == 1:
            # Get the repo name
            repo=line
            repo_names = normalize_repo(repo)
            new_repo = repo_names[2]
        else:
            repo = repos[0]
            repo_names = normalize_repo(repos[1])
            registry = repo_names[0]
            ns = repo_names[1]
            new_repo = repo_names[2]
        sync_repo(client, registry, ns, insecure_registry, repo, new_repo)
    except Exception:
        logging.exception("processing line %s" % line)

for i in range(nt):
    queue_pull.put(None)
for i in range(nt):
    queue_push.put(None)

for t in pull_threads:
    t.join()
