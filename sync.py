#!/usr/bin/env python

from http.server import BaseHTTPRequestHandler, HTTPServer
import uuid
import json
import copy
import re
import logging
import sys

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')

# kubectl delivery image passed in as argv[1]
kubectl_image = 'mpioperator/kubectl-delivery:latest'
if len(sys.argv) > 1:
  kubectl_image = sys.argv[1]

def deep_merge_lists(original, incoming, alwaysAdd=False):
    """
    Deep merge two lists. Modifies original.
    Reursively call deep merge on each correlated element of list. 
    If item type in both elements are
     a. dict: call deep_merge_dicts on both values.
     b. list: Calls deep_merge_lists on both values.
     c. any other type: Value is overridden.
     d. conflicting types: Value is overridden.

    If length of incoming list is more that of original then extra values are appended.
    """
    if not alwaysAdd:
        common_length = min(len(original), len(incoming))
        for idx in range(common_length):
            if isinstance(original[idx], dict) and isinstance(incoming[idx], dict):
                deep_merge_dicts(original[idx], incoming[idx])

            elif isinstance(original[idx], list) and isinstance(incoming[idx], list):
                deep_merge_lists(original[idx], incoming[idx])

            else:
                original[idx] = incoming[idx]

        for idx in range(common_length, len(incoming)):
            original.append(incoming[idx])
    else:
        for idx in range(0, len(incoming)):
            original.append(incoming[idx])


def deep_merge_dicts(original, incoming):
    """
    Deep merge two dictionaries. Modfies original.
    For key conflicts if both values are:
     a. dict: Recursivley call deep_merge_dicts on both values.
     b. list: Calls deep_merge_lists on both values.
     c. any other type: Value is overridden.
     d. conflicting types: Value is overridden.

    """
    special = ['volumeMounts', 'volumes']

    for key in incoming:
        if key in original:
            if isinstance(original[key], dict) and isinstance(incoming[key], dict):
                deep_merge_dicts(original[key], incoming[key])

            elif isinstance(original[key], list) and isinstance(incoming[key], list):
                deep_merge_lists(original[key], incoming[key], (True if key in special else False))

            else:
                original[key] = incoming[key]
        else:
            original[key] = incoming[key]


def is_job_finished(job):
  for condition in job.get('status', {}).get('conditions', []):
    if (condition['type'] == 'Complete' or condition['type'] == 'Failed') and condition['status'] == 'True':
      return True
  return False

def get_index(base_name, name):
  m = re.match(r'^(.*)-(\d+)$', name)
  if m and m.group(1) == base_name:
    return int(m.group(2))
  return -1

def new_pod(job, index):
  pod = copy.deepcopy(job['spec']['template'])
  pod['apiVersion'] = 'v1'
  pod['kind'] = 'Pod'
  pod['metadata'] = pod.get('metadata', {})
  pod['metadata']['name'] = '%s-%d' % (job['metadata']['name'], index)

  # Add env var to every container.
  for container in pod['spec']['containers']:
    env = container.get('env', [])
    env.append({'name': 'JOB_INDEX', 'value': str(index)})
    container['env'] = env

  return pod

def build_name(job):
  container = (job['metadata']['name'] if 'name' in job['metadata'] else str(uuid.uuid4()))
  name = 'mpioperator-%s' % container
  return name

def new_mpiset(job, name):
  if not name:
    name = build_name(job)
  replicas = int(job['spec']['replicas'] if 'replicas' in job['spec'] else 1)
  image = 'gitlab.catalyst.net.nz:4567/piers/k8s-hack/kube-grid:opr'
  if 'image' in job['spec']:
    image = job['spec']['image']

  mpiset = {
    'apiVersion': 'apps/v1',
    'kind': 'StatefulSet',
    'metadata': {
      'labels': {
        'group_name': 'skatelescope.org',
        'mpi_job_name': name,
        'mpi_role_type': 'worker'
        },
      'name': '%s-worker' % name
      },
    'spec': {
      'podManagementPolicy': 'Parallel',
      'replicas': replicas,
      'revisionHistoryLimit': 10,
      'selector': {
        'matchLabels': {
          'group_name': 'skatelescope.org',
          'mpi_job_name': name,
          'mpi_role_type': 'worker' 
          }
        },
      'serviceName': '%s-worker' % name,
      'template': {
        'metadata': {
          'labels': {
            'group_name': 'skatelescope.org',
            'mpi_job_name': name,
            'mpi_role_type': 'worker' 
            } 
          },
        'spec': {
          'containers': [
           {'args': [
              '365d' 
              ],
            'command': [
              'sleep' 
              ],
            'image': image,
            'imagePullPolicy': 'IfNotPresent',
            'name': 'mpiexecutor',
            'resources': {
              'limits': {
                'nvidia.com/gpu': "0" 
                } 
              },
            'terminationMessagePath': '/dev/termination-log',
            'terminationMessagePolicy': 'File',
            'volumeMounts': [
              {'mountPath': '/etc/mpi',
              'name': 'mpi-job-config'
              } 
              ] 
            } 
            ],
          'dnsPolicy': 'ClusterFirst',
          'restartPolicy': 'Always',
          'schedulerName': 'default-scheduler',
          'securityContext': {},
          'terminationGracePeriodSeconds': 30,
          'volumes': [
           { 'configMap': {
              'defaultMode': 420,
              'items': [
               {'key': 'kubexec.sh',
                'mode': 365,
                'path': 'kubexec.sh'
                } 
                ],
              'name': configmap_name(job)
              },
            'name': 'mpi-job-config' 
            } 
            ] 
          } 
        },
      'updateStrategy': {
        'rollingUpdate': {
          'partition': 0
          }
        },
        'type': 'RollingUpdate'
    }
  }
  template = copy.deepcopy(job['spec']['template'])

  # must remove overriding command and args or we will have trouble
  if 'spec' in template and 'containers' in template['spec'] and len(template['spec']['containers']) > 0:
    for i in range(len(template['spec']['containers'])):
      if 'command' in template['spec']['containers'][i]:
        del(template['spec']['containers'][i]['command'])
      if 'args' in template['spec']['containers'][i]:
        del(template['spec']['containers'][i]['args'])
  
  logging.debug("mpiset Template: " + repr(template))
  target = copy.deepcopy(mpiset['spec']['template'])
  logging.debug("mpiset Target: " + repr(target))
  res = deep_merge_dicts(target, template)
  logging.debug("mpiset Update Target: " + repr(target))
  logging.debug("mpiset Result: " + repr(res))
  mpiset['spec']['template'] = target
  return mpiset

def configmap_name(job):
  return'%s-config' % build_name(job)

def new_mpiserviceaccount(job, name, jobname):
  if not name:
    name = build_name(job)
  if not jobname:
    jobname = jobname_name(job)

  serviceaccount = {
    'apiVersion': 'v1',
    'kind': 'ServiceAccount',
    'metadata': {
      'labels': {
        'group_name': 'skatelescope.org',
        'mpi_job_name': name,
        'app': name
        },
      'name': jobname 
      }
    }
  return serviceaccount

def new_mpirole(job, name, jobname):
  if not name:
    name = build_name(job)
  if not jobname:
    jobname = jobname_name(job)

  replicas = int(job['spec']['replicas'] if 'replicas' in job['spec'] else 1)
  hostfile = ["%s-worker-%d" % (name, i) for i in range(replicas)]

  role = {
    'apiVersion': 'rbac.authorization.k8s.io/v1',
    'kind': 'Role',
    'metadata': {
      'labels': {
        'group_name': 'skatelescope.org',
        'mpi_job_name': name,
        'app': name
        },
      'name': jobname 
      },
    'rules': [
      {'apiGroups': [
        ""
        ],
      'resourceNames': hostfile,
      'resources': [
        'pods'
        ],
      'verbs': [
        'get'
        ] 
        },
      {'apiGroups': [
        ""
        ],
      'resourceNames': hostfile,
      'resources': [
        'pods/exec'
        ],
      'verbs':[
        'create'
        ]
        }
      ]
    }
  return role

def new_mpirolebinding(job, name, jobname):
  if not name:
    name = build_name(job)
  if not jobname:
    jobname = jobname_name(job)

  rolebinding = {
    'apiVersion': 'rbac.authorization.k8s.io/v1',
    'kind': 'RoleBinding',
    'metadata': {
      'labels': {
        'group_name': 'skatelescope.org',
        'mpi_job_name': name,
        'app': name
        },
      'name': jobname 
      },
    'roleRef': {
      'apiGroup': 'rbac.authorization.k8s.io',
      'kind': 'Role',
      'name': jobname },
     'subjects': [
      { 'kind': 'ServiceAccount',
        'name': jobname } 
        ]
    }
  return rolebinding

def new_configmap(job, name, configname):
  if not name:
    name = build_name(job)
  if not configname:
    configname = configmap_name(job)

  replicas = int(job['spec']['replicas'] if 'replicas' in job['spec'] else 1)
  hostfile = "\n".join(["%s-worker-%d slots=1" % (name, i) for i in range(replicas)])

  configmap = {
    'apiVersion': 'v1',
    'data': {
        'hostfile': hostfile,
        'kubexec.sh': 
          "#!/bin/sh\n" +
          "set -x\n" +
          "POD_NAME=$1\n" +
          "shift\n" +
          "/opt/kube/kubectl exec ${POD_NAME} -- /bin/sh -c \"$*\"\n" 
      },
    'kind': 'ConfigMap',
    'metadata': {
      'name': configname 
      }
    }
  return configmap

def jobname_name(job):
  return'%s-launcher' % build_name(job)

def new_mpilauncher(job, name, configname, jobname):
  if not name:
    name = build_name(job)
  if not configname:
    configname = configmap_name(job)
  if not jobname:
    jobname = jobname_name(job)
  image = 'gitlab.catalyst.net.nz:4567/piers/k8s-hack/kube-grid:opr'
  if 'image' in job['spec']:
    image = job['spec']['image']

  mpijob = {
    'apiVersion': 'batch/v1',
    'kind': 'Job',
    'metadata': {
      'labels': {
        'group_name': 'kubeflow.org',
        'mpi_job_name': name,
        'mpi_role_type': 'launcher'
        },
      'name': jobname,
      },
    'spec': {
      'backoffLimit': 6,
      'completions': 1,
      'parallelism': 1,
      'template': {
        'metadata': {
          'labels': {
            'group_name': 'skatelescope.org',
            'job-name': jobname,
            'mpi_job_name': name,
            'mpi_role_type': 'launcher' 
            },
          },
        'spec': {
          'containers': [
          {
            'env': [
             { 'name': 'OMPI_MCA_plm_rsh_agent',
              'value': '/etc/mpi/kubexec.sh' },
             { 'name': 'OMPI_MCA_orte_default_hostfile',
              'value': '/etc/mpi/hostfile' }
              ],
            'image': image,
            'imagePullPolicy': 'IfNotPresent',
            'name': 'test1',
            'resources': {},
            'terminationMessagePath': '/dev/termination-log',
            'terminationMessagePolicy': 'File',
            'volumeMounts': [
             { 'mountPath': '/opt/kube',
              'name': 'mpi-job-kubectl' },
             { 'mountPath': '/etc/mpi',
              'name': 'mpi-job-config'}
              ]
              }
              ],
          'dnsPolicy': 'ClusterFirst',
          'initContainers': [
           { 'env': [
              { 'name': 'TARGET_DIR',
                'value': '/opt/kube'} ],
            'image': kubectl_image,
            'imagePullPolicy': 'Always',
            'name': 'kubectl-delivery',
            'resources': {},
            'terminationMessagePath': '/dev/termination-log',
            'terminationMessagePolicy': 'File',
            'volumeMounts': [
              { 'mountPath': '/opt/kube',
              'name': 'mpi-job-kubectl' }
              ]
            }
          ],
          'restartPolicy': 'Never',
          'schedulerName': 'default-scheduler',
          'securityContext': {},
          'serviceAccount': jobname,
          'serviceAccountName': jobname,
          'terminationGracePeriodSeconds': 30,
          'volumes': [
            { 'emptyDir': {},
            'name': 'mpi-job-kubectl' },
            { 'configMap': {
              'defaultMode': 420,
              'items': [
                { 'key': 'kubexec.sh',
                'mode': 365,
                'path': 'kubexec.sh' },
                { 'key': 'hostfile',
                'mode': 292,
                'path': 'hostfile' } ],
              'name': configname },
            'name': 'mpi-job-config' } 
            ] 
          } 
        } 
      }
    }       
  template = copy.deepcopy(job['spec']['template'])
  logging.debug("mpijob Template: " + repr(template))
  target = copy.deepcopy(mpijob['spec']['template'])
  logging.debug("mpijob Target: " + repr(target))
  res = deep_merge_dicts(target, template)
  logging.debug("mpijob Update Target: " + repr(target))
  logging.debug("mpijob Result: " + repr(res))
  mpijob['spec']['template'] = target
  return mpijob


class Controller(BaseHTTPRequestHandler):
  def sync(self, job, children):
    logging.debug("Job in: " + repr(job))
    logging.debug("Children in: " + repr(children))

    configname = False
    for mpiconfig_name, mpiconfig in children['ConfigMap.v1'].items():
      configname = mpiconfig_name

    jobname = False
    job_state = ''
    job_status = ''
    job_succeeded = ''
    for mpijob_name, mpijob in children['Job.batch/v1'].items():
      jobname = mpijob_name
      if mpijob.get('status', {}).get('active', 0) == 1:
          job_state = 'Running'
          job_succeeded = 'Unknown'
          job_status = 'Unknown'
      for condition in mpijob.get('status', {}).get('conditions', []):
        if (condition['type'] == 'Complete' or condition['type'] == 'Failed') and condition['status'] == 'True':
          job_state = 'Finished'
          job_succeeded = ("succeeded" if mpijob.get('status', {}).get('succeeded', 0) == 1 else "Failed")
        else:
          job_state = 'Running'
          job_succeeded = 'Unknown'
        job_status = condition['type']

    desired_status = {
      'currentReplicas': 0, 
      'readyReplicas': 0, 
      'replicas': 0,
      'job': {}
      }

    name = False
    worker_suffix = "-worker"
    for mpiset_name, mpiset in children['StatefulSet.apps/v1'].items():
      if mpiset_name.endswith(worker_suffix):
        name = mpiset_name[:-len(worker_suffix)]
      desired_status['currentReplicas'] = mpiset.get('status', {}).get('currentReplicas')
      desired_status['readyReplicas'] = mpiset.get('status', {}).get('readyReplicas')
      desired_status['replicas'] = mpiset.get('status', {}).get('replicas')
    desired_status['job'] = {'state': job_state, 'status': job_status, 'success': job_succeeded}

    mpiset = new_mpiset(job, name)
    mpiconfig = new_configmap(job, name, configname)
    mpiserviceaccount = new_mpiserviceaccount(job, name, jobname)
    mpirole = new_mpirole(job, name, jobname)
    mpirolebinding = new_mpirolebinding(job, name, jobname)
    mpijob = new_mpilauncher(job, name, configname, jobname)

    return {'status': desired_status, 'children': [mpiserviceaccount,
                                                   mpirole, 
                                                   mpirolebinding, 
                                                   mpiconfig, 
                                                   mpiset, 
                                                   mpijob]}


# we only handle POST requests
  def do_POST(self):
    observed = json.loads(self.rfile.read(int(self.headers.get('content-length'))))
    desired = self.sync(observed['parent'], observed['children'])

    self.send_response(200)
    self.send_header('Content-type', 'application/json')
    self.end_headers()
    logging.debug("out: " + str(json.dumps(desired)))
    self.wfile.write(json.dumps(desired).encode())

# boot the web server
HTTPServer(('', 80), Controller).serve_forever()
