import docker
import time
import json
import attr
from pathlib import Path
from collections import defaultdict

import pytest
import testinfra

import logging

import test.helper as h
from .helper import (
    fixture_builder,
    safe_filename,
    PRVSNR_REPO_INSTALL_DIR, mock_system_cmd, restore_system_cmd
)

logger = logging.getLogger(__name__)

MODULE_DIR = Path(__file__).resolve().parent

DOCKER_IMAGES_REPO = "seagate/ees-prvsnr"
VAGRANT_VMS_PREFIX = DOCKER_IMAGES_REPO.replace('/', '.')

SSH_KEY_FILE_NAME = "id_rsa.test"

# TODO 'base' level format is too different
ENV_LEVELS_HIERARCHY = {
    'base': {
        'centos7.5.1804': {
            'docker': 'centos:7.5.1804',
            'vagrant': {
                'vm_box_name': 'geerlingguy/centos7',
                'vm_box_version': '1.2.7'
            },
            'host': 'centos:7.5.1804'
        },
        'centos7.7.1908': {
            'docker': 'centos:7.7.1908',
            'vagrant': {
                'vm_box_name': 'geerlingguy/centos7',
                'vm_box_version': '1.2.17'
            },
            'host': 'centos:7.7.1908'
        }
    },
    'repos-installed': 'base',
    'salt-installed': 'repos-installed',
    'singlenode-prvsnr-installed': {
        'parent': 'salt-installed',
        'docker': {
            'build_type': 'commit',
            'scripts': [
                "cd {repo_path}/cli/src && bash ../../images/vagrant/setup_prvsnr_singlenode.sh local ''",
                "cd {repo_path}/cli/src && bash ../../images/vagrant/setup_prvsnr_api_env.sh",
                "rm -rf {repo_path}"
            ]
        }
    },
    'singlenode-test-integration-ready': 'singlenode-prvsnr-installed',
    # only for vbox
    'singlenode-deploy-ready': {
        'parent': 'salt-installed',
        'vars': ['prvsnr_src', 'prvsnr_release', 'eos_release']
    },
    'singlenode-eos-deployed': 'singlenode-deploy-ready',
    'singlenode-eos-ready': 'singlenode-eos-deployed',

    # utility levels
    'rpmbuild': 'base',
    'utils': 'base',
    'network-manager-installed': 'base',

    # bvt
    'singlenode-bvt-ready': {
        'parent': 'base',
        'vars': ['prvsnr_cli_release', 'prvsnr_release', 'eos_release']
    }
}


BASE_OS_NAMES = list(ENV_LEVELS_HIERARCHY['base'])
DEFAULT_BASE_OS_NAME = 'centos7.7.1908'

DEFAULT_EOS_SPEC = {
    'eosnode1': {
        'hostname': 'eosnode-1',
        'minion_id': 'eosnode-1',
        'is_primary': True,
    }, 'eosnode2': {
        'hostname': 'eosnode-2',
        'minion_id': 'eosnode-2',
        'is_primary': False,
    }
}


@attr.s
class HostMeta:
    # TODO validators for all
    remote = attr.ib()
    host = attr.ib()
    ssh_config = attr.ib()
    request = attr.ib()

    label = attr.ib(converter=lambda v: '' if not v else v, default='')
    machine_name = attr.ib(default=None)
    hostname = attr.ib(default=None)
    iface = attr.ib(default=None)

    _hostname = attr.ib(init=False, default=None)
    _tmpdir = attr.ib(init=False, default=None)
    _repo = attr.ib(init=False, default=None)
    _rpm_prvsnr = attr.ib(init=False, default=None)
    _rpm_prvsnr_cli = attr.ib(init=False, default=None)

    def __attrs_post_init__(self):
        # TODO more smarter logic to get iface that is asseccible from host
        # (relates to https://github.com/hashicorp/vagrant/issues/2779)
        if self.iface is None:
            if (
                isinstance(self.remote, h.VagrantMachine) and
                (self.remote.provider == 'vbox')
            ):
                self.iface = 'enp0s8'
            else:
                self.iface = 'eth0'

        assert self.host.interface(self.iface).exists

    @property
    def hostname(self):
        if self._hostname is None:
            self._hostname = self.host.check_output('hostname')
        return self._hostname

    @property
    def tmpdir(self):
        if self._tmpdir is None:
            tmpdir_function = self.request.getfixturevalue('tmpdir_function')
            # TODO non linux systems
            self._tmpdir = Path('/tmp') / tmpdir_function.relative_to('/')
            self.host.check_output("mkdir -p {}".format(self._tmpdir))
        return self._tmpdir

    def copy_from_host(self, host_path, local_path=None):
        tmpdir_local = self.request.getfixturevalue('tmpdir_function')

        if local_path is None:
            local_path = tmpdir_local / host_path.name
        else:
            local_path = local_path.resolve()
            local_path.relative_to(tmpdir_local)  # ensure that it's inside tmpdir

        local_path.parent.mkdir(parents=True, exist_ok=True)

        h.localhost.check_output(
            "scp -r -F {} {}:{} {}".format(
                self.ssh_config,
                self.hostname,
                host_path,
                local_path
            )
        )
        return local_path

    def copy_to_host(self, local_path, host_path=None):
        if host_path is None:
            host_path = self.tmpdir / local_path.name

        self.host.check_output(
            "mkdir -p {}".format(host_path.parent)
        )

        h.localhost.check_output(
            "scp -r -F {} {} {}:{}".format(
                self.ssh_config,
                local_path,
                self.hostname,
                host_path
            )
        )
        return host_path

    @property
    def rpm_prvsnr(self):
        if self._rpm_prvsnr is None:
            rpm_local_path = self.request.getfixturevalue('rpm_prvsnr')
            self._rpm_prvsnr = self.copy_to_host(rpm_local_path)
        return self._rpm_prvsnr

    @property
    def rpm_prvsnr_cli(self):
        if self._rpm_prvsnr_cli is None:
            rpm_local_path = self.request.getfixturevalue('rpm_prvsnr_cli')
            self._rpm_prvsnr_cli = self.copy_to_host(rpm_local_path)
        return self._rpm_prvsnr_cli

    @property
    def repo(self):
        if self._repo is None:
            repo_tgz = self.request.getfixturevalue('repo_tgz')
            self._repo = h.inject_repo(
                self.host, self.ssh_config, repo_tgz
            )
        return self._repo

    @property
    def fixture_name(self):
        return 'mhost' + self.label

    def run(self, script, *args, force_dump=False, **kwargs):
        return h.run(self.host, script, *args, force_dump=force_dump, **kwargs)

    def check_output(self, script, *args, force_dump=False, **kwargs):
        return h.check_output(self.host, script, *args, force_dump=force_dump, **kwargs)


class LocalHostMeta(HostMeta):
    @property
    def hostname(self):
        return 'localhost'

    @property
    def tmpdir(self):
        if self._tmpdir is None:
            self._tmpdir = self.request.getfixturevalue('tmpdir_function')
        return self._tmpdir

    @property
    def rpm_prvsnr(self):
        if self._rpm_prvsnr is None:
            self._rpm_prvsnr = self.request.getfixturevalue('rpm_prvsnr')
        return self._rpm_prvsnr

    @property
    def rpm_prvsnr_cli(self):
        if self._rpm_prvsnr is None:
            self._rpm_prvsnr = self.request.getfixturevalue('rpm_prvsnr_cli')
        return self._rpm_prvsnr

    @property
    def repo(self):
        if self._repo is None:
            self._repo = h.PROJECT_PATH
        return self._repo

    @property
    def fixture_name(self):
        return 'mlocalhost'


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "env_provider(string): mark test to be run "
                   "in the environment provided by the provider"
    )
    config.addinivalue_line(
        "markers", "env_level(string): mark test to be run "
                   "in the specific environment level"
    )
    config.addinivalue_line(
        "markers", "eos_spec(dict): mark test as expecting "
                   "specific EOS stack configuration, default: {}"
                   .format(json.dumps(DEFAULT_EOS_SPEC))
    )
    config.addinivalue_line(
        "markers", "hosts(list): mark test as expecting "
                   "the specified list of hosts by labels, "
                   "default: ['']"
    )
    config.addinivalue_line(
        "markers", "isolated: mark test to be run in the isolated "
                   "environment instead of module wide shared"
    )
    config.addinivalue_line(
        "markers", "inject_ssh_config: mark test as expecting ssh configuration "
                   "only for specified hosts, default: all hosts"
    )
    config.addinivalue_line(
        "markers", "mock_cmds(dict[host_lable, list]): mark test as requiring "
                   "list of mocked system commands per host"
    )
    config.addinivalue_line(
        "markers", "vagrant_default_ssh(string): mark test to use "
                   "default ssh config provided by vagrant, "
                   "default: True for single node env, False otherwise"
    )
    config.addinivalue_line(
        "markers", "eos_bvt: mark test as BVT one"
    )


prvsnr_pytest_options = {
    # TODO might be useful to test in multiple at once
    "base-env": dict(
        action='store', choices=BASE_OS_NAMES,
        default=DEFAULT_BASE_OS_NAME,
        help="base OS name, defaults to {}".format(DEFAULT_BASE_OS_NAME)
    ),
    "env-provider": dict(
        action='store',
        choices=['host', 'docker', 'vbox'],
        default='docker',
        help="test environment provider, defaults to docker"
    ),
    "prvsnr-src": dict(
        action='store', choices=['rpm', 'gitlab', 'local'],
        default='rpm',
        help="Provisioner source to use, defaults to 'rpm'"
    ),
    "prvsnr-cli-release": dict(
        action='store', default='integration/centos-7.7.1908/last_successful',
        help="Provisioner cli release to use, defaults to 'integration/centos-7.7.1908/last_successful'"
    ),
    "prvsnr-release": dict(
        action='store', default='integration/centos-7.7.1908/last_successful',
        help="Provisioner release to use, defaults to 'integration/centos-7.7.1908/last_successful'"
    ),
    "eos-release": dict(
        action='store', default='integration/centos-7.7.1908/last_successful',
        help="Target EOS release to verify, defaults to 'integration/centos-7.7.1908/last_successful'"
    )
}


def pytest_addoption(parser):
    for name, params in prvsnr_pytest_options.items():
        parser.addoption("--" + name, **params)


@pytest.fixture(scope="session")
def options_list():
    return list(prvsnr_pytest_options)


@pytest.fixture(scope="session", autouse=True)
def dump_options(request, options_list):
    opts_str = '\n'.join([
        '{}: {}'.format(opt, request.config.getoption(opt.replace('-', '_')))
        for opt in options_list
    ])
    logger.info('Passed options:\n{}'.format(opts_str))


@pytest.fixture(scope="session")
def project_path():
    return h.PROJECT_PATH


@pytest.fixture(scope="session")
def docker_client():
    return docker.from_env()


@pytest.fixture(scope="session")
def localhost():
    return h.localhost


@pytest.fixture(scope='session')
def ssh_key(tmpdir_session):
    key = tmpdir_session / SSH_KEY_FILE_NAME
    with open(str(MODULE_DIR / SSH_KEY_FILE_NAME)) as f:
        key.write_text(f.read())
    key.chmod(0o600)
    return key


@pytest.fixture(scope="session")
def vagrant_global_status_prune(localhost):
    localhost.check_output('vagrant global-status --prune')


@pytest.fixture(scope="session")
def base_env(request):
    return request.config.getoption("base_env")


@pytest.fixture
def env_provider(request):
    res = request.config.getoption("env_provider")
    marker = request.node.get_closest_marker('env_provider')
    if marker:
        res = marker.args[0]
    return res


@pytest.fixture(scope='session')
def hosts_spec(request):
    return {
        'eosnode1': {
            'remote': {
                'hostname': 'eosnode-1',
                'specific': {
                    'vbox': {
                        'memory': 4096,
                        'cpus': 2,
                        'mgmt_disk_size': 2048,
                        'data_disk_size': 2048
                    }
                }
            },
            'minion_id': 'eosnode-1',
            'is_primary': True,
        }, 'eosnode2': {
            'remote': {
                'hostname': 'eosnode-2',
                'specific': {
                    'vbox': {
                        'memory': 4096,
                        'cpus': 2,
                        'mgmt_disk_size': 2048,
                        'data_disk_size': 2048
                    }
                }
            },
            'minion_id': 'eosnode-2',
            'is_primary': False,
        }
    }


# TODO multi platforms case
@pytest.fixture(scope="session")
def vbox_seed_machine(request, base_env, project_path, vagrant_global_status_prune):
    env_spec = ENV_LEVELS_HIERARCHY['base'][base_env]['vagrant']
    # TODO separator ???
    machine_name = '_'.join([VAGRANT_VMS_PREFIX, base_env, 'seed'])
    vagrantfile_name = "Vagrantfile.seed"
    vagrantfile = project_path / 'images' / 'vagrant' / vagrantfile_name

    machine = h.VagrantMachine(
        machine_name, vagrantfile=vagrantfile, user_vars={
            'VAR_VM_NAME': machine_name,
            'VAR_VM_BOX_NAME': env_spec['vm_box_name'],
            'VAR_VM_BOX_VERSION': env_spec['vm_box_version']
        }
    )

    logger.info(
        "Preparing VirtualBox {} seed machine"
        .format(base_env)
    )
    status = machine.status(update=True)
    # TODO move that specific to VagrantMachine API
    if status['state'][0] == 'not_created':
        # create VM
        machine.up()
        machine.cmd('halt')
        machine.cmd('snapshot', 'save', machine.name, 'initial --force')
    else:
        # TODO do that only if rebuild base vagrant box
        machine.cmd('snapshot', 'restore', machine.name, 'initial --no-start')

    return machine


# TODO
#  - DOCS
#  - make configurable to use specific git state (git archive)
@pytest.fixture(scope='session')
def repo_tgz(project_path, localhost, tmpdir_session):
    res = tmpdir_session / 'repo.tgz'
    excluded_dirs = ['--exclude="{}"'.format(d) for d in h.REPO_BUILD_DIRS]
    localhost.check_output(
        'tar -czf "{}" {} -C "{}" .'
        .format(res, ' '.join(excluded_dirs), project_path)
    )
    return res


def _rpmbuild_mhost(request):
    env_provider = request.config.getoption("env_provider")
    base_env = request.config.getoption("base_env")

    # TODO DOCS : example how to run machine out of fixture scope
    remote = build_remote(
        env_provider, request, base_env, 'rpmbuild'
    )

    try:
        return discover_remote(request, remote)
    except Exception:
        remote.destoy()
        raise


def _copy_to_local(mhost, host_path, tmpdir_local):
    local_path = tmpdir_local / host_path.name
    h.localhost.check_output(
        "scp -r -F {} {}:{} {}".format(
            mhost.ssh_config,
            mhost.hostname,
            host_path,
            local_path
        )
    )
    return local_path


def rpm_prvsnr_build(mhost):
    mhost.check_output(
        'cd {} && sh -x devops/rpms/buildrpm.sh'.format(mhost.repo)
    )
    return Path(
        mhost.check_output(
            'ls ~/rpmbuild/RPMS/x86_64/{}*.rpm | grep -v debug'
            .format(h.PRVSNR_PKG_NAME)
        )
    )


def rpm_prvsnr_cli_build(mhost):
    mhost.check_output(
        'cd {} && sh -x cli/buildrpm.sh'.format(mhost.repo)
    )
    return Path(
        mhost.check_output(
            'ls ~/rpmbuild/RPMS/x86_64/{}*.rpm | grep -v debug'
            .format(h.PRVSNR_CLI_PKG_NAME)
        )
    )


@pytest.fixture(scope='session')
def rpm_build():
    def _f(request, tmpdir_local, cli=False, mhost_init_cb=None):
        mhost = _rpmbuild_mhost(request)
        # TODO DOCS : example how to run machine out of fixture scope
        with mhost.remote as _:
            if mhost_init_cb:
                mhost_init_cb(mhost)
            rpm_remote_path = (
                rpm_prvsnr_cli_build(mhost) if cli else rpm_prvsnr_build(mhost)
            )
            return _copy_to_local(
                mhost, rpm_remote_path, tmpdir_local
            )
    return _f


@pytest.fixture(scope='session')
def rpm_prvsnr(request, tmpdir_session, rpm_build):
    return rpm_build(request, tmpdir_session, cli=False)


@pytest.fixture(scope='session')
def rpm_prvsnr_cli(request, tmpdir_session, rpm_build):
    return rpm_build(request, tmpdir_session, cli=True)


@pytest.fixture(autouse=True)
def log_test_name(request):
    logger.debug('Test started: {}'.format(request.node.nodeid))
    yield
    logger.debug('Test finished: {}'.format(request.node.nodeid))


@pytest.fixture(scope='module')
def env_level():
    return 'base'


def env_fixture_suffix(os_name, env_level):
    return "{}_{}".format(os_name, env_level.replace('-', '_'))


def build_docker_image_fixture(os_name, env_level):

    def docker_image(request, docker_client, project_path, tmpdir_session):
        env_spec = ENV_LEVELS_HIERARCHY[env_level]
        docker_spec = {}
        build_type = 'dockerfile'

        if env_level == 'base':
            parent_env_level = None
        elif type(env_spec) is dict:
            parent_env_level = env_spec['parent']
            docker_spec = env_spec.get('docker', {})
            build_type = docker_spec.get('build_type', 'dockerfile')
        else:
            parent_env_level = env_spec

        def _image_short_id(image):
            return image.short_id.replace('sha256:', '')

        def _image_tag(image, os_name, env_level):
            # TODO better parts separator
            return '-'.join([os_name, env_level, _image_short_id(image)])

        def _image_name(image, os_name, env_level):
            return (
                "{}:{}".format(
                    DOCKER_IMAGES_REPO, _image_tag(image, os_name, env_level)
                )
            )

        def _parent_image_name():
            if env_level == 'base':
                # TODO image object would be necessary for 'commit' built_type
                return env_spec[os_name]['docker'], None
            else:
                p_image = request.getfixturevalue(
                    "docker_image_{}".format(
                        env_fixture_suffix(os_name, parent_env_level)
                    )
                )
                p_image_name = _image_name(p_image, os_name, parent_env_level)
                if p_image_name not in p_image.tags:
                    logger.warning(
                        "parent image {} doesn't have expected tag {}"
                        .format(p_image, p_image_name)
                    )
                    # TODO what if no tags at all, do we allow referencing by id ?
                    p_image_name = p_image.tags[0]
                return p_image_name, p_image

        parent_image_name, parent_image = _parent_image_name()

        # build image
        logger.info(
            "Building docker env '{}' for base env '{}'"
            .format(env_level, os_name)
        )
        if build_type == 'dockerfile':
            df_name = "Dockerfile.{}".format(env_level)
            dockerfile = project_path / 'images' / 'docker' / df_name
            dockerfile_tmpl = dockerfile.parent / (dockerfile.name + '.tmpl')

            if dockerfile_tmpl.exists():
                dockerfile_str = dockerfile_tmpl.read_text().format(parent=parent_image_name)
            else:
                dockerfile_str = dockerfile.read_text()

            dockerfile = tmpdir_session / dockerfile.name
            dockerfile.write_text(dockerfile_str)
            image = h._docker_image_build(docker_client, dockerfile, ctx=project_path)
        else:  # image as container commit
            assert parent_image is not None
            remote = build_remote(
                'docker',
                request,
                os_name,
                env_level=parent_env_level,
                base_level=parent_image
            )

            try:
                mhost = discover_remote(request, remote)
            except Exception:
                remote.destoy()
                raise

            with mhost.remote as _:
                # apply scripts to running container
                for script in docker_spec.get('scripts', []):
                    mhost.check_output(script.format(repo_path=mhost.repo))
                # commit it to image
                image = h._docker_container_commit(remote)

        # set image name
        if _image_name(image, os_name, env_level) not in image.tags:
            try:
                image.tag(DOCKER_IMAGES_REPO, tag=_image_tag(image, os_name, env_level))
            except Exception:
                # ensure that image doesn't have any other tags
                # TODO what if it actually was tagged but failed then,
                #      is it possible in docker API
                if not image.tags:
                    docker_client.images.remove(image.id, force=False, noprune=False)
                raise
            else:
                image.reload()

        return image

    fixture_builder(
        'session',
        suffix=('_' + env_fixture_suffix(os_name, env_level)),
        module_name=__name__,
        name_with_scope=False
    )(docker_image)


for _os_name in BASE_OS_NAMES:
    for _env_level in ENV_LEVELS_HIERARCHY:
        build_docker_image_fixture(_os_name, _env_level)


def build_vagrant_box_fixture(os_name, env_level):

    # returns VagrantBox object, box is already added to vagrant
    def vagrant_box(request, base_env, project_path):

        def _get_user_vars():
            res = {'base_env': base_env}

            if env_level == 'base':
                p_machine = request.getfixturevalue('vbox_seed_machine')
                res['seed_vm_name'] = p_machine.name
            else:
                env_spec = ENV_LEVELS_HIERARCHY[env_level]
                if type(env_spec) is dict:
                    p_env = env_spec['parent']
                    # add pytest session level vars from config
                    for vname in env_spec.get('vars', []):
                        res[vname] = request.config.getoption(vname)
                else:
                    p_env = env_spec

                if type(p_env) is not str:
                    raise RuntimeError(
                        "{} is not a string: {}"
                        .format(type(p_env), p_env)
                    )

                p_box = request.getfixturevalue(
                    "vagrant_box_{}".format(
                        env_fixture_suffix(os_name, p_env))
                )
                res['parent_source'] = res['parent_box_name'] = p_box.name
                res['skip_add'] = 'true'

            return res

        def _check_box_added(box_name):
            vagrant_rows = h.Vagrant().box("list", parse=True)
            for row in vagrant_rows:
                if (row.data_type == 'box-name') and (row.data[0] == box_name):
                    return True
            return False

        def _add_box(path, name):
            box = h.VagrantBox(name, path=path)
            logger.info(
                "Adding vagrant box '{}'".format(name)
            )
            h.Vagrant().box(
                "add --provider virtualbox --name",
                box.name,
                '--force',
                "'{}'".format(box.path)
            )
            return box

        user_vars = _get_user_vars()

        # TODO separator ???
        box_name = '_'.join([VAGRANT_VMS_PREFIX, base_env, env_level])
        box_path = project_path / '.boxes' / base_env / env_level / 'package.box'

        # TODO for now always rebuild the base box if box file is missed
        #  until smarter logic of boxes rebuild is implemented,
        #  should be triggered when any of the following is true:
        #  - parent env is changed
        #  - input user variables set is changed
        #  - provisioning scripts and other related sources are changed
        if _check_box_added(box_name) and False:
            logger.info(
                "Vagrant box for env '{}:{}' already added"
                .format(base_env, env_level)
            )
            # TODO ??? do not specify package.box here since we don't
            # know whether it exists or not and how it is really related
            # to the box
            return h.VagrantBox(box_name)

        if not box_path.exists():
            pf_name = "packer.{}.json".format(env_level)
            packerfile = project_path / 'images' / 'vagrant' / pf_name
            packer = h.Packer(packerfile)

            logger.info(
                "Building vagrant env '{}' for base env '{}'"
                .format(env_level, base_env)
            )
            # TODO pytest options to turn on packer debug
            packer.build(
                '--force {}'.format(
                    ' '.join([
                        '-var {}={}'.format(k, v) for k, v in user_vars.items()
                    ])
                )
            )

        if not box_path.exists():
            raise RuntimeError(
                "Vagrant box file {} hasn't been created".format(box_path)
            )

        return _add_box(box_path, box_name)

    fixture_builder(
        'session',
        suffix=('_' + env_fixture_suffix(os_name, env_level)),
        module_name=__name__,
        name_with_scope=False
    )(vagrant_box)


for _os_name in BASE_OS_NAMES:
    for _env_level in ENV_LEVELS_HIERARCHY:
        build_vagrant_box_fixture(_os_name, _env_level)

'''
def build_vagrant_machine_shared_fixture(os_name, env_level):

    def vagrant_machine_shared(request, project_path, tmp_path_factory):
        box = request.getfixturevalue(
            "vagrant_box_{}".format(
                env_fixture_suffix(os_name, env_level))
        )
        machine_name = safe_vagrant_machine_name("{}-shared".format(box.name))
        tmpdir = tmp_path_factory.mktemp(safe_filename(machine_name))
        vagrantfile_dest = str(tmpdir / 'Vagrantfile')
        machine = VagrantMachine(
            machine_name, box=box, vagrantfile_dest=vagrantfile_dest
        )
        try:
            machine.destroy()
            machine.up()
            machine.cmd('halt')
            # TODO hardcoded
            machine.cmd('snapshot', 'save', machine.name, 'initial --force')
            machine.cmd('halt --force')
            yield machine
        finally:
            machine.destroy()

    fixture_builder(
        'session',
        suffix=('_' + env_fixture_suffix(os_name, env_level)),
        module_name=__name__,
        name_with_scope=False
    )(vagrant_machine_shared)


for _os_name in BASE_OS_NAMES:
    for _env_level in ENV_LEVELS_HIERARCHY:
        build_vagrant_machine_shared_fixture(_os_name, _env_level)


@fixture_builder(['module', 'function'], module_name=__name__)
def vagrant_machine_shared(request, env_level, project_path):
    if request.scope == 'function':
        marker = request.node.get_closest_marker('env_level')
        if marker:
            env_level = marker.args[0]

    _parts = env_level.split('-')
    os_name = _parts[0]
    env_level = '-'.join(_parts[1:])

    return request.getfixturevalue(
        "vagrant_machine_shared_{}".format(env_fixture_suffix(os_name, env_level))
    )
'''


@pytest.fixture(scope='module')
def post_host_run_hook():
    def f(mhost):
        pass
    return f


@pytest.fixture(scope='module')
def vagrantfile_tmpl():
    return MODULE_DIR / 'Vagrantfile.tmpl'


def docker_container(request, docker_client, base_env, env_level, hosts_spec):
    # TODO API to reuse by other providers
    if request.scope == 'function':
        marker = request.node.get_closest_marker('env_level')
        if marker:
            env_level = marker.args[0]

    label = request.fixturename[len('docker_container_{}'.format(request.scope)):]

    try:
        hostname = hosts_spec[label]['remote']['hostname']
    except KeyError:
        hostname = None

    try:
        remote_opts = hosts_spec[label]['remote']['specific']['docker']
    except KeyError:
        remote_opts = {}

    with build_remote(
        'docker', request, base_env, env_level, label=label,
        hostname=hostname, specific=remote_opts
    ) as remote:
        yield remote


def vagrant_machine(
    request, base_env, env_level, vagrantfile_tmpl, hosts_spec
):
    # TODO API to reuse by other providers
    if request.scope == 'function':
        marker = request.node.get_closest_marker('env_level')
        if marker:
            env_level = marker.args[0]

        marker = request.node.get_closest_marker('vagrantfile_tmpl')
        if marker:
            vagrantfile_tmpl = marker.args[0]

    label = request.fixturename[len('vagrant_machine_{}'.format(request.scope)):]

    try:
        hostname = hosts_spec[label]['remote']['hostname']
    except KeyError:
        hostname = None

    try:
        remote_opts = hosts_spec[label]['remote']['specific']['vbox']
    except KeyError:
        remote_opts = {}

    # TODO configurable way for that
    shared = False
    if shared:
        machine = request.getfixturevalue(
            "vagrant_machine_shared_{}".format(request.scope)
        )
        try:
            # TODO hardcoded
            machine.cmd('snapshot', 'restore', machine.name, 'initial --no-provision')
            yield machine
        finally:
            machine.cmd('halt', '--force')
    else:
        with build_remote(
            'vbox', request, base_env, env_level, label=label,
            vagrantfile_tmpl=vagrantfile_tmpl,
            hostname=hostname, specific=remote_opts
        ) as remote:
            yield remote
            logger.info(
                "Destroying remote '{}'".format(remote.name)
            )


@pytest.fixture(scope='session')
def tmpdir_session(request, tmp_path_factory):
    return tmp_path_factory.getbasetemp()


@pytest.fixture(scope='module')
def tmpdir_module(request, tmp_path_factory):
    return tmp_path_factory.mktemp(safe_filename(request.node.nodeid))


@pytest.fixture
def tmpdir_function(request, tmpdir_module):
    res = tmpdir_module / safe_filename(request.node.name)
    res.mkdir()
    return res


@pytest.fixture
def ssh_config(request, tmpdir_function):
    return tmpdir_function / "ssh_config"


@pytest.fixture
def hosts(request):
    hosts = []
    #marker = request.node.get_closest_marker('hosts')
    #if marker:
    #    hosts = marker.args[0]
    hosts.append(request.config.getoption('eos-node1'))
    #hosts.append(request.config.getoption('eos-node2'))
    return hosts


@pytest.fixture
def hosts_meta():
    return {}


@pytest.fixture
def mock_hosts(hosts, request, mlocalhost):
    mocked = defaultdict(list)

    marker = request.node.get_closest_marker('mock_cmds')
    if not marker:
        return

    try:
        for label, cmds in marker.args[0].items():
            if label in hosts:
                fixture_name = 'mhost' + label
                mhost = request.getfixturevalue(fixture_name)
                assert mhost is not mlocalhost  # TODO more clever check
                for cmd in cmds:
                    mock_system_cmd(mhost.host, cmd)
                    mocked[fixture_name].append(cmd)
        yield
    finally:
        for fixture_name, cmds in mocked.items():
            mhost = request.getfixturevalue(fixture_name)
            for cmd in cmds:
                restore_system_cmd(mhost.host, cmd)


@pytest.fixture
def inject_ssh_config(hosts, mlocalhost, ssh_config, ssh_key, request):
    target_hosts = set(hosts)
    ssh_config_paths = {}
    def_ssh_config = '~/.ssh/config'

    marker = request.node.get_closest_marker('inject_ssh_config')
    if marker:
        target_hosts = set(marker.args[0]) & set(hosts)

    for label in target_hosts:
        mhost = request.getfixturevalue('mhost' + label)
        assert mhost is not mlocalhost
        for path in (ssh_config, ssh_key):
            mhost.check_output("mkdir -p {}".format(path.parent))
            mlocalhost.check_output(
                "scp -p -F {} {} {}:{}".format(
                    ssh_config, path, mhost.hostname, path
                )
            )
        mhost.check_output("mkdir -p ~/.ssh")
        mhost.check_output('cp -p {} {}'.format(ssh_config, def_ssh_config))
        ssh_config_paths[label] = Path(
            mhost.check_output('realpath {}'.format(def_ssh_config))
        )
    return ssh_config_paths


@pytest.fixture
def eos_spec(request):
    marker = request.node.get_closest_marker('eos_spec')
    spec = marker.args[0] if marker else DEFAULT_EOS_SPEC
    node1 = request.config.getoption('eos-node1')
    node2 = request.config.getoption('eos-node2')
    if node1 != 'eosnode1':
        DEFAULT_EOS_SPEC['eosnode1']['hostname'] = node1
        DEFAULT_EOS_SPEC['eosnode2']['hostname'] = node2
        spec = DEFAULT_EOS_SPEC
    return spec


# eos_spec sanity checks
@pytest.fixture
def _eos_spec(eos_spec):
    assert len({k: v for k, v in eos_spec.items() if v['is_primary']}) == 1
    return eos_spec


@pytest.fixture
def eos_hosts(hosts, _eos_spec, request):
    _hosts = defaultdict(dict)
    for label in hosts:
        if label in _eos_spec:
            _hosts[label]['minion_id'] = _eos_spec[label]['minion_id']
            _hosts[label]['is_primary'] = _eos_spec[label].get('is_primary', True)

    return _hosts


@pytest.fixture
def eos_primary_host_label(eos_hosts):
    return [k for k, v in eos_hosts.items() if v['is_primary']][0]


@pytest.fixture
def eos_primary_mhost(eos_primary_host_label, request):
    return request.getfixturevalue('mhost' + eos_primary_host_label)


@pytest.fixture
def eos_primary_host_ip(eos_primary_mhost):
    return eos_primary_mhost.host.interface(
        eos_primary_mhost.iface
    ).addresses[0]


@pytest.fixture
def install_provisioner(eos_hosts, mlocalhost, project_path, ssh_config, request):
    assert eos_hosts, "the fixture makes sense only for eos hosts"

    for label in eos_hosts:
        mhost = request.getfixturevalue('mhost' + label)
        mlocalhost.check_output(
            "bash -c \". {script_path} && install_provisioner {repo_src} {prvsnr_version} {hostspec} "
            "{ssh_config} {sudo} {singlenode}\""
            .format(
                script_path=(project_path / 'cli/src/functions.sh'),
                repo_src='local',
                prvsnr_version="''",
                hostspec=mhost.hostname,
                ssh_config=ssh_config,
                sudo='false',
                singlenode=('true' if len(eos_hosts) == 1 else 'false')
            )
        )


@pytest.fixture
def configure_salt(eos_hosts, install_provisioner, eos_primary_host_ip, request):
    cli_dir = PRVSNR_REPO_INSTALL_DIR / 'cli' / 'src'

    for label, host_spec in eos_hosts.items():
        minion_id = host_spec['minion_id']
        is_primary = (
            'true' if host_spec['is_primary'] else 'false'
        )
        primary_host = (
            "localhost" if host_spec['is_primary'] else eos_primary_host_ip
        )
        mhost = request.getfixturevalue('mhost' + label)
        mhost.check_output(
            ". {} && configure_salt '{}' '' '' '' {} {}".format(
                cli_dir / 'functions.sh', minion_id, is_primary, primary_host
            )
        )


@pytest.fixture
def accept_salt_keys(eos_hosts, install_provisioner, eos_primary_mhost):
    cli_dir = PRVSNR_REPO_INSTALL_DIR / 'cli' / 'src'

    for label, host_spec in eos_hosts.items():
        eos_primary_mhost.check_output(". {} && accept_salt_key '{}'".format(
            cli_dir / 'functions.sh', host_spec['minion_id'])
        )

    eos_primary_mhost.check_output("salt '*' mine.update")


@pytest.fixture(callable=True)
def mlocalhost(localhost, request):
    return LocalHostMeta(None, localhost, None, request, label=None, iface='lo')


@pytest.fixture
def vagrant_default_ssh(request, hosts):
    res = (len(hosts) < 2)
    marker = request.node.get_closest_marker('vagrant_default_ssh')
    if marker:
        res = marker.args[0]
    return res


def build_remote(
    env_provider, request, os_name, env_level, label=None, base_level=None, **kwargs
):
    base_name = h.remote_name(
        request.node.nodeid, request.scope, os_name, env_level, label=label
    )

    if base_level is None:
        logger.info(
            "Building env-level '{}' for os '{}' with provider '{}'"
            .format(env_level, os_name, env_provider)
        )
        # TODO return an object of a class
        if env_provider == 'docker':
            base_level = request.getfixturevalue(
                "docker_image_{}".format(env_fixture_suffix(os_name, env_level))
            )
        elif env_provider == 'vbox':
            base_level = request.getfixturevalue(
                "vagrant_box_{}".format(env_fixture_suffix(os_name, env_level))
            )
        else:
            raise ValueError("unknown env provider {}".format(env_provider))

    tmpdir = request.getfixturevalue('tmpdir_' + request.scope)

    logger.info(
        "Starting remote with base name '{}'".format(base_name)
    )
    remote = h.run_remote(env_provider, base_level, base_name, tmpdir, **kwargs)
    logger.info(
        "Started remote '{}'".format(remote.name)
    )
    return remote


def discover_remote(
    request, remote, ssh_config=None, host_fixture_label=None
):
    tmpdir = request.getfixturevalue('tmpdir_{}'.format(request.scope))

    _host = None
    _iface = 'eth0'
    _ssh_config = None
    if isinstance(remote, h.Container):
        # update container data
        remote.container.reload()
        _host = testinfra.get_host(remote.container.id, connection='docker')

        #  ssh to be up
        service = _host.service
        #   TODO service name is 'ssh' on debian, ubuntu, ... ???
        service_name = 'sshd'
        while not service(service_name).is_running:
            time.sleep(.5)
        # TODO verify that eth0 is always true for docker
    elif isinstance(remote, h.VagrantMachine):
        # in vagrant ssh-config a machine is accessible via localhost:localport,
        # use that as a temporary way to get its own ip and access
        # its internal ssh port
        _ssh_config_tmp = tmpdir / "ssh_config.{}.tmp".format(remote.name)
        with _ssh_config_tmp.open('w') as f:
            f.write(remote.ssh_config())

        # FIXME sometimes hostonlynetwork of the vbox machine is not up properly
        # (route table is not created), no remedy found yet, possible workaround
        # is to remove vbox hostonly network for a machine and re-create the machine
        # https://jts.seagate.com/browse/EOS-3129

        # vagrant uses vagrant machine name as Host ID in ssh-config
        _host = testinfra.get_host(
            "ssh://{}".format(remote.name), ssh_config=str(_ssh_config_tmp)
        )
        _iface = 'enp0s8'
        if request.getfixturevalue('vagrant_default_ssh'):
            _ssh_config = remote.ssh_config().replace(remote.name, remote.hostname)

    else:
        raise ValueError(
            "unexpected remote type: {}".format(type(remote))
        )

    # TODO there might be some cheaper way (e.g. get from testinfra host object)
    _hostname = _host.check_output('hostname')

    if ssh_config is None:
        ssh_config = tmpdir / 'ssh_config.{}'.format(remote.name)
    ssh_config.touch(mode=0o600, exist_ok=True)

    # prepare ssh configuration
    if _ssh_config is None:
        ssh_key = request.getfixturevalue('ssh_key')
        _ssh_config = (
                "Host {}\n"
                "  Hostname {}\n"
                "  Port 22\n"
                "  User {}\n"
                "  UserKnownHostsFile /dev/null\n"
                "  StrictHostKeyChecking no\n"
                "  IdentityFile {}\n"
                "  IdentitiesOnly yes\n"
                "  LogLevel FATAL\n"
            ).format(
                _hostname,
                _host.interface(_iface).addresses[0],
                'root',
                str(ssh_key)
            )

    with ssh_config.open('a') as f:
        f.write(_ssh_config)

    hostspec = "ssh://{}".format(_hostname)
    _host = testinfra.get_host(hostspec, ssh_config=str(ssh_config))

    return HostMeta(
        remote,
        _host,
        ssh_config,
        request,
        label=host_fixture_label,
        machine_name=remote.name,
        iface=_iface
    )


def build_mhost_fixture(label=None, module_name=__name__):
    remote_fixtures = {'module': {}, 'function': {}}
    for scope in ('module', 'function'):
        remote_fixtures[scope]['docker'] = fixture_builder(
            scope,
            suffix=label,
            module_name=module_name
        )(docker_container)

        remote_fixtures[scope]['vagrant'] = fixture_builder(
            scope,
            suffix=label,
            module_name=module_name
        )(vagrant_machine)

    def host(request):
        label = request.fixturename[len('host'):]
        _mhost_fixture_name = 'mhost' + label
        # ensure that related host fixture has been actually called
        mhost = request.getfixturevalue(_mhost_fixture_name)
        return mhost.host

    fixture_builder(
        'function',
        suffix=label,
        module_name=module_name,
        name_with_scope=False
    )(host)

    @fixture_builder(
        "function", suffix=label, module_name=module_name,
        name_with_scope=False
    )
    def mhost(localhost, request, tmpdir_function, hosts_meta, env_provider):
        if env_provider == 'host':
            return request.getfixturevalue('mlocalhost')

        scope = (
            'function' if request.node.get_closest_marker('isolated')
            else 'module'
        )

        ssh_config = request.getfixturevalue('ssh_config')

        logger.info(
            "Building remote '{}' using provider '{}'"
            .format('default' if label is None else label, env_provider)
        )

        if env_provider == 'docker':
            remote = request.getfixturevalue(
                remote_fixtures[scope]['docker'].__name__
            )
        else:
            # TODO skip if vagrant or packer or virtualbox is not installed
            # pytest.skip()
            remote = request.getfixturevalue(
                remote_fixtures[scope]['vagrant'].__name__
            )

        logger.info(
            "Discovering remote '{}'".format(remote.name)
        )
        meta = discover_remote(
            request, remote,
            ssh_config=ssh_config, host_fixture_label=label
        )

        hosts_meta[meta.fixture_name] = meta

        # TODO add try-catch and remove default implementation of post_host_run_hook
        request.getfixturevalue('post_host_run_hook')(meta)

        return meta


# default 'host' fixture is always present
build_mhost_fixture()
# also host fixtures for EOS stack makes sense
build_mhost_fixture('eosnode1')
build_mhost_fixture('eosnode2')
