#!/usr/bin/env python
"""
sync is a minimal HTTP server that responds to POST requests from
the MetaController for the configuration of MPIJob objects in Kubernetes
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import uuid
import json
import copy
import logging
import sys
WORKER_SUFFIX = "-worker"

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')

# kubectl delivery image passed in as argv[1]
KUBECTL_IMAGE = 'mpioperator/kubectl-delivery:latest'
if len(sys.argv) > 1:
    KUBECTL_IMAGE = sys.argv[1]


def deep_merge_lists(original, incoming, alwaysadd=False):
    """
    Deep merge two lists. Modifies original.
    Reursively call deep merge on each correlated element of list.
    If item type in both elements are
     a. dict: call deep_merge_dicts on both values.
     b. list: Calls deep_merge_lists on both values.
     c. any other type: Value is overridden.
     d. conflicting types: Value is overridden.

    If length of incoming list is more that of original then extra
     values are appended.
    """
    if not alwaysadd:
        common_length = min(len(original), len(incoming))
        for idx in range(common_length):
            if isinstance(original[idx], dict) and \
               isinstance(incoming[idx], dict):
                deep_merge_dicts(original[idx], incoming[idx])

            elif (isinstance(original[idx], list) and
                  isinstance(incoming[idx], list)):
                deep_merge_lists(original[idx], incoming[idx])

            else:
                original[idx] = incoming[idx]

        for idx in range(common_length, len(incoming)):
            original.append(incoming[idx])
    else:
        for idx, _ in enumerate(incoming):
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
            if isinstance(original[key], dict) and \
               isinstance(incoming[key], dict):
                deep_merge_dicts(original[key], incoming[key])

            elif (isinstance(original[key], list) and
                  isinstance(incoming[key], list)):
                deep_merge_lists(original[key], incoming[key],
                                 (True if key in special else False))

            else:
                original[key] = incoming[key]
        else:
            original[key] = incoming[key]


def build_name(job):
    """
    Generate the Job name
    """
    container = (job['metadata']['name']
                 if 'name' in job['metadata'] else str(uuid.uuid4()))
    name = 'mpioperator-%s' % container
    return name


def new_mpiset(job, name):
    """
    Create the MPI StatefulSet
    This creates a series of Pods that use an MPI enabled image
    to build a cluster

    The spec section of the MPIJob definition is used to
    decorate the container for things like volumes/mounts etc.
    """
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
            'labels': {'group_name': 'skatelescope.org',
                       'mpi_job_name': name,
                       'mpi_role_type': 'worker'},
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
                    'mpi_role_type': 'worker'}
                },
            'serviceName': '%s-worker' % name,
            'template': {
                'metadata': {
                    'labels': {
                        'group_name': 'skatelescope.org',
                        'mpi_job_name': name,
                        'mpi_role_type': 'worker'}
                },
                'spec': {
                    'containers': [
                        {'args': ['365d'],
                         'command': ['sleep'],
                         'image': image,
                         'imagePullPolicy': 'IfNotPresent',
                         'name': 'mpiexecutor',
                         'resources': {
                             'limits': {'nvidia.com/gpu': "0"}
                         },
                         'terminationMessagePath': '/dev/termination-log',
                         'terminationMessagePolicy': 'File',
                         'volumeMounts': [
                             {'mountPath': '/etc/mpi',
                              'name': 'mpi-job-config'}]
                         }
                    ],
                    'dnsPolicy': 'ClusterFirst',
                    'restartPolicy': 'Always',
                    'schedulerName': 'default-scheduler',
                    'securityContext': {},
                    'terminationGracePeriodSeconds': 30,
                    'volumes': [
                        {'configMap': {
                            'defaultMode': 420,
                            'items': [
                                {'key': 'kubexec.sh',
                                 'mode': 365,
                                 'path': 'kubexec.sh'}
                            ],
                            'name': configmap_name(job)},
                         'name': 'mpi-job-config'}
                    ]
                }
            },
            'updateStrategy': {
                'rollingUpdate': {'partition': 0}
            },
            'type': 'RollingUpdate'
        }
    }
    template = copy.deepcopy(job['spec']['template'])

    # must remove overriding command and args or we will have trouble
    if 'spec' in template and 'containers' in template['spec']:
        if template['spec']['containers']:
            for i in range(len(template['spec']['containers'])):
                if 'command' in template['spec']['containers'][i]:
                    del template['spec']['containers'][i]['command']
                if 'args' in template['spec']['containers'][i]:
                    del template['spec']['containers'][i]['args']

    logging.debug("mpiset Template: %s", repr(template))
    target = copy.deepcopy(mpiset['spec']['template'])
    logging.debug("mpiset Target: %s", repr(target))
    deep_merge_dicts(target, template)
    logging.debug("mpiset Update Target: %s", repr(target))
    mpiset['spec']['template'] = target
    return mpiset


def configmap_name(job):
    """
    Generate config map name
    """
    return'%s-config' % build_name(job)


def new_mpiserviceaccount(job, name, jobname):
    """
    MPI launcher serviceaccount for kubectl access to worker Pods
    """
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
    """
    Create a role so that the launcher can discover worker details
    """
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
            {'apiGroups': [""],
             'resourceNames': hostfile,
             'resources': ['pods'],
             'verbs': ['get']
             },
            {'apiGroups': [""],
             'resourceNames': hostfile,
             'resources': ['pods/exec'],
             'verbs':['create']
             }
            ]
        }
    return role


def new_mpirolebinding(job, name, jobname):
    """
    Bind the MPI role to the service account
    """
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
            'name': jobname},
        'subjects': [
            {'kind': 'ServiceAccount',
             'name': jobname}
        ]
        }
    return rolebinding


def new_configmap(job, name, configname):
    """
    Construct the config map that contains the worker Pod details
    """
    if not name:
        name = build_name(job)
    if not configname:
        configname = configmap_name(job)

    replicas = int(job['spec']['replicas'] if 'replicas' in job['spec'] else 1)
    hostfile = "\n".join(["%s-worker-%d slots=1" % (name, i)
                          for i in range(replicas)])

    configmap = {
        'apiVersion': 'v1',
        'data': {'hostfile': hostfile,
                 'kubexec.sh':
                 "#!/bin/sh\n" +
                 "set -x\n" +
                 "POD_NAME=$1\n" +
                 "shift\n" +
                 "/opt/kube/kubectl exec ${POD_NAME} -- /bin/sh -c \"$*\"\n"},
        'kind': 'ConfigMap',
        'metadata': {
            'name': configname
            }
        }
    return configmap


def jobname_name(job):
    """
    Generate the Job name
    """
    return'%s-launcher' % build_name(job)


def new_mpilauncher(job, name, configname, jobname):  # pylint: too-many-locals
    """
    Create the MPI Job
    This creates a Pod that use an MPI enabled image to run mpiexec or mpirun

    The spec section of the MPIJob definition is used to
    decorate the container for things like volumes/mounts etc.
    """
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
                        {'env': [
                            {'name': 'OMPI_MCA_plm_rsh_agent',
                             'value': '/etc/mpi/kubexec.sh'},
                            {'name': 'OMPI_MCA_orte_default_hostfile',
                             'value': '/etc/mpi/hostfile'}],
                         'image': image,
                         'imagePullPolicy': 'IfNotPresent',
                         'name': 'test1',
                         'resources': {},
                         'terminationMessagePath': '/dev/termination-log',
                         'terminationMessagePolicy': 'File',
                         'volumeMounts': [{'mountPath': '/opt/kube',
                                           'name': 'mpi-job-kubectl'},
                                          {'mountPath': '/etc/mpi',
                                           'name': 'mpi-job-config'}]
                         }
                    ],
                    'dnsPolicy': 'ClusterFirst',
                    'initContainers': [
                        {'env': [
                            {'name': 'TARGET_DIR',
                             'value': '/opt/kube'}],
                         'image': KUBECTL_IMAGE,
                         'imagePullPolicy': 'Always',
                         'name': 'kubectl-delivery',
                         'resources': {},
                         'terminationMessagePath': '/dev/termination-log',
                         'terminationMessagePolicy': 'File',
                         'volumeMounts': [{'mountPath': '/opt/kube',
                                           'name': 'mpi-job-kubectl'}]
                         },
                        # Need to check all of cluster is Running
                        {'name': 'check-cluster-up',
                         'env': [
                             {'name': 'TARGET_DIR',
                              'value': '/opt/kube'}],
                         'image': 'busybox:latest',
                         'imagePullPolicy': 'Always',
                         'command': ['sh', '-e', '-c',
                                     'for i in `cat /etc/mpi/hostfile | \
                                      cut -f1 -d" "`; do echo $i; \
                                      /opt/kube/kubectl get pod $i -o yaml | \
                                      grep phase: | grep Running; done'],
                         'volumeMounts': [{'mountPath': '/opt/kube',
                                           'name': 'mpi-job-kubectl'},
                                          {'mountPath': '/etc/mpi',
                                           'name': 'mpi-job-config'}],
                         'resources': {},
                         'terminationMessagePath': '/dev/termination-log',
                         'terminationMessagePolicy': 'File'}],
                    'restartPolicy': 'Never',
                    'schedulerName': 'default-scheduler',
                    'securityContext': {},
                    'serviceAccount': jobname,
                    'serviceAccountName': jobname,
                    'terminationGracePeriodSeconds': 30,
                    'volumes': [
                        {'emptyDir': {},
                         'name': 'mpi-job-kubectl'},
                        {'configMap': {
                            'defaultMode': 420,
                            'items': [
                                {'key': 'kubexec.sh',
                                 'mode': 365,
                                 'path': 'kubexec.sh'},
                                {'key': 'hostfile',
                                 'mode': 292,
                                 'path': 'hostfile'}],
                            'name': configname},
                         'name': 'mpi-job-config'}]
                    }
                }
            }
        }
    template = copy.deepcopy(job['spec']['template'])
    logging.debug("mpijob Template: %s", repr(template))
    target = copy.deepcopy(mpijob['spec']['template'])
    logging.debug("mpijob Target: %s", repr(target))
    deep_merge_dicts(target, template)
    logging.debug("mpijob Update Target: %s", repr(target))
    mpijob['spec']['template'] = target
    return mpijob


class Controller(BaseHTTPRequestHandler):
    """
    Basic HHTP controller - handles only POST requests
    """
    def sync(self, job, children):  # pylint: disable=no-self-use
        """
        Synchronise the incoming MPIJob request by generating
        Kubernetes object specifications
        """
        logging.debug("Job in: %s", repr(job))
        logging.debug("Children in: %s", repr(children))

        configname = False
        for mpiconfig_name, _ in children['ConfigMap.v1'].items():
            configname = mpiconfig_name

        job_status = {'name': False,
                      'state': "",
                      'status': "",
                      'succeeded': ""}
        for mpijob_name, mpijob in children['Job.batch/v1'].items():
            job_status['name'] = mpijob_name
            if mpijob.get('status', {}).get('active', 0) == 1:
                job_status['state'] = 'Running'
                job_status['succeeded'] = 'Unknown'
                job_status['status'] = 'Unknown'
            for condition in mpijob.get('status', {}).get('conditions', []):
                if (condition['type'] == 'Complete' or
                        condition['type'] == 'Failed') and \
                   condition['status'] == 'True':
                    job_status['state'] = 'Finished'
                    job_status['succeeded'] = \
                        ("succeeded" if
                         mpijob.get('status', {}).get('succeeded', 0) == 1
                         else "Failed")
                else:
                    job_status['state'] = 'Running'
                    job_status['succeeded'] = 'Unknown'
                job_status['status'] = condition['type']

        desired_status = {
            'currentReplicas': 0,
            'readyReplicas': 0,
            'replicas': 0,
            'job': {}
            }

        name = False
        for mpiset_name, mpiset in children['StatefulSet.apps/v1'].items():
            if mpiset_name.endswith(WORKER_SUFFIX):
                name = mpiset_name[:-len(WORKER_SUFFIX)]
            desired_status['currentReplicas'] = \
                mpiset.get('status', {}).get('currentReplicas')
            desired_status['readyReplicas'] = \
                mpiset.get('status', {}).get('readyReplicas')
            desired_status['replicas'] = \
                mpiset.get('status', {}).get('replicas')
        desired_status['job'] = {'state': job_status['state'],
                                 'status': job_status['status'],
                                 'success': job_status['succeeded']}

        return {'status': desired_status, 'children':
                [new_mpiserviceaccount(job, name, job_status['name']),
                 new_mpirole(job, name, job_status['name']),
                 new_mpirolebinding(job, name, job_status['name']),
                 new_configmap(job, name, configname),
                 new_mpiset(job, name),
                 new_mpilauncher(job, name, configname, job_status['name'])]}


# we only handle POST requests
    def do_POST(self):  # pylint: disable=invalid-name
        """
        the POST responder...
        """
        observed = json.loads(self.rfile.read(
            int(self.headers.get('content-length'))))
        desired = self.sync(observed['parent'], observed['children'])

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        logging.debug("out: %s", str(json.dumps(desired)))
        self.wfile.write(json.dumps(desired).encode())


# boot the web server
HTTPServer(('', 80), Controller).serve_forever()
