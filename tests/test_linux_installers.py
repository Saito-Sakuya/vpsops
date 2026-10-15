"""Linux installer regression checks using POSIX sh and isolated command stubs."""

import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "install.sh"


@unittest.skipUnless(sys.platform == "linux", "the Linux installer runs under POSIX sh")
class LinuxInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="vpsops-linux-installer-"))
        self.bin_dir = self.temp / "stub commands"
        self.bin_dir.mkdir()
        self.tmp_dir = self.temp / "installer temp"
        self.tmp_dir.mkdir()
        self.log = self.temp / "commands.log"

    def tearDown(self):
        shutil.rmtree(self.temp)

    def add_stub(self, name, body):
        path = self.bin_dir / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def env(self, **extra):
        return {
            **os.environ,
            "PATH": str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "TMPDIR": str(self.tmp_dir),
            "VPSOPS_TEST_LOG": str(self.log),
            **{key: str(value) for key, value in extra.items()},
        }

    def root_and_apt_stubs(self):
        self.add_stub("id", 'if [ "${1-}" = -u ]; then printf "0\\n"; exit 0; fi\nexit 1\n')
        self.add_stub("apt-get", 'printf "apt-get %s\\n" "$*" >> "$VPSOPS_TEST_LOG"\nexit 0\n')

    def run_script(self, *args, env=None):
        return subprocess.run(
            ["/bin/sh", str(INSTALLER), *args],
            env=env or self.env(),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_help_and_invalid_invocations(self):
        help_result = self.run_script("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("install.sh agent", help_result.stdout)

        for args in (("unknown",), ("--help", "extra"), ("gateway", "extra")):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("用法：", result.stderr)

    def test_invalid_key_and_ipv4_are_rejected_before_apt(self):
        self.root_and_apt_stubs()
        bad_inputs = (
            ("ssh-ed25519 AAAA\ninjected", "100.64.0.5", "必须是单行文本"),
            ("ssh-ed25519 AAAA key", "100.64.0.256", "每段必须在 0 到 255 之间"),
        )
        for public_key, jump_ip, expected_error in bad_inputs:
            with self.subTest(jump_ip=jump_ip):
                self.log.unlink(missing_ok=True)
                result = self.run_script("agent", public_key, jump_ip)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn(expected_error, result.stderr)
                self.assertFalse(self.log.exists(), "apt-get must not run for invalid input")

    def test_download_failure_stops_before_agent_script_and_cleans_temp_directory(self):
        self.root_and_apt_stubs()
        self.add_stub(
            "curl",
            'printf "curl %s\\n" "$*" >> "$VPSOPS_TEST_LOG"\nexit 22\n',
        )
        marker = self.temp / "agent-called"
        result = self.run_script(
            "agent",
            "ssh-ed25519 AAAA key",
            "100.64.0.5",
            env=self.env(VPSOPS_AGENT_MARKER=marker),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("下载 vpsops 源码失败", result.stderr)
        self.assertIn("apt-get update", self.log.read_text(encoding="utf-8"))
        self.assertFalse(marker.exists(), "the downloaded VPS installer must not run")
        self.assertEqual(list(self.tmp_dir.iterdir()), [], "installer temp directory must be removed")

    def make_agent_archive(self):
        payload = self.temp / "payload" / "vpsops-master" / "deploy" / "vps" / "install.sh"
        payload.parent.mkdir(parents=True)
        payload.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$VPSOPS_AGENT_ARGS"\n'
            'printf "called\\n" > "$VPSOPS_AGENT_MARKER"\n',
            encoding="utf-8",
        )
        archive = self.temp / "vpsops.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(payload, arcname="vpsops-master/deploy/vps/install.sh")
        return archive

    def test_agent_success_downloads_extracts_passes_arguments_and_cleans_temp(self):
        self.root_and_apt_stubs()
        archive = self.make_agent_archive()
        self.add_stub(
            "curl",
            'destination=\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  if [ "$1" = --output ]; then destination=$2; shift 2; else shift; fi\n'
            'done\n'
            '[ -n "$destination" ] || exit 2\n'
            'cp "$VPSOPS_TEST_ARCHIVE" "$destination"\n',
        )
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA example-key comment"
        jump_ip = "100.64.0.5"
        args_path = self.temp / "agent args"
        marker = self.temp / "agent-called"
        result = self.run_script(
            "agent",
            key,
            jump_ip,
            env=self.env(
                VPSOPS_TEST_ARCHIVE=archive,
                VPSOPS_AGENT_ARGS=args_path,
                VPSOPS_AGENT_MARKER=marker,
            ),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(marker.is_file())
        self.assertEqual(args_path.read_text(encoding="utf-8").splitlines(), [key, jump_ip])
        self.assertEqual(list(self.tmp_dir.iterdir()), [], "installer temp directory must be removed")

    def add_gateway_stubs(self, state_dir):
        state_dir.mkdir(parents=True)
        group_marker = state_dir / "group-created"
        user_marker = state_dir / "user-created"
        self.add_stub(
            "id",
            'if [ "${1-}" = -u ]; then printf "0\\n"; exit 0; fi\n'
            'if [ "${1-}" = vpsops ]; then [ -f "$VPSOPS_TEST_USER_MARKER" ]; exit $?; fi\n'
            'exit 1\n',
        )
        self.add_stub(
            "getent",
            '[ "${1-}" = group ] && [ "${2-}" = vpsops ] && '
            '[ -f "$VPSOPS_TEST_GROUP_MARKER" ]\n',
        )
        self.add_stub(
            "apt-get",
            'printf "apt-get %s\\n" "$*" >> "$VPSOPS_TEST_LOG"\nexit 0\n',
        )
        self.add_stub(
            "git",
            'printf "git %s\\n" "$*" >> "$VPSOPS_TEST_LOG"\n'
            'if [ "${1-}" = clone ]; then\n'
            '  destination=\n'
            '  for argument do destination=$argument; done\n'
            '  mkdir -p "$destination/deploy/gateway"\n'
            '  printf "example gateway config\\n" > "$destination/deploy/gateway/gateway.example.yaml"\n'
            '  printf "unit file\\n" > "$destination/deploy/gateway/vpsops-gateway.service"\n'
            '  mkdir -p "$destination/.git"\n'
            '  exit 0\n'
            'fi\n'
            'if [ "${1-}" = -C ]; then shift 2; fi\n'
            'case "${1-}:${2-}:${3-}" in\n'
            '  rev-parse:--is-inside-work-tree:*) printf "true\\n" ;;\n'
            '  config:--get:remote.origin.url) printf "https://github.com/Saito-Sakuya/vpsops.git\\n" ;;\n'
            '  symbolic-ref:--quiet:--short) printf "master\\n" ;;\n'
            '  status:--porcelain:--untracked-files=all) : ;;\n'
            '  fetch:origin:master) : ;;\n'
            '  merge:--ff-only:FETCH_HEAD) : ;;\n'
            '  *) exit 70 ;;\n'
            'esac\n',
        )
        self.add_stub("uv", 'printf "uv %s\\n" "$*" >> "$VPSOPS_TEST_LOG"\nexit 0\n')
        self.add_stub(
            "groupadd",
            'printf "groupadd %s\\n" "$*" >> "$VPSOPS_TEST_LOG"\n'
            ': > "$VPSOPS_TEST_GROUP_MARKER"\n',
        )
        self.add_stub(
            "useradd",
            'printf "useradd %s\\n" "$*" >> "$VPSOPS_TEST_LOG"\n'
            ': > "$VPSOPS_TEST_USER_MARKER"\n',
        )
        self.add_stub(
            "ssh-keygen",
            'printf "ssh-keygen %s\\n" "$*" >> "$VPSOPS_TEST_LOG"\n'
            'key_path=\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  if [ "$1" = -f ]; then key_path=$2; shift 2; else shift; fi\n'
            'done\n'
            '[ -n "$key_path" ] || exit 2\n'
            'printf "generated private key\\n" > "$key_path"\n'
            'printf "generated public key\\n" > "$key_path.pub"\n',
        )
        self.add_stub(
            "install",
            'printf "install %s\\n" "$*" >> "$VPSOPS_TEST_LOG"\n'
            'directory=0\n'
            'while [ "$#" -gt 0 ]; do\n'
            '  case "$1" in\n'
            '    -d) directory=1; shift ;;\n'
            '    -o|-g|-m) shift 2 ;;\n'
            '    --) shift; break ;;\n'
            '    *) break ;;\n'
            '  esac\n'
            'done\n'
            'if [ "$directory" -eq 1 ]; then mkdir -p "$1"; exit $?; fi\n'
            'source=$1\ndestination=$2\n'
            '[ -n "$source" ] && [ -n "$destination" ] || exit 2\n'
            'case "$destination" in\n'
            '  "$VPSOPS_TEST_CONFIG_DIR"/*)\n'
            '    mkdir -p "$(dirname "$destination")"\n'
            '    if [ "$source" = /dev/null ]; then : > "$destination"; else cp "$source" "$destination"; fi\n'
            '    ;;\n'
            '  /etc/systemd/system/*)\n'
            '    mkdir -p "$VPSOPS_TEST_ROOT/systemd"\n'
            '    cp "$source" "$VPSOPS_TEST_ROOT/systemd/${destination##*/}"\n'
            '    ;;\n'
            '  *) printf "refusing destination: %s\\n" "$destination" >&2; exit 70 ;;\n'
            'esac\n',
        )
        for name in ("chown", "chmod", "systemctl"):
            self.add_stub(
                name,
                f'printf "{name} %s\\n" "$*" >> "$VPSOPS_TEST_LOG"\nexit 0\n',
            )
        self.add_stub("curl", 'printf "unexpected curl %s\\n" "$*" >> "$VPSOPS_TEST_LOG"\nexit 70\n')
        self.add_stub("sudo", 'printf "unexpected sudo\\n" >&2\nexit 70\n')
        return group_marker, user_marker

    def run_gateway_functions(self, repo_dir, config_dir, state_dir):
        source = INSTALLER.read_text(encoding="utf-8")
        marker = 'if [ "${1-}" = --help ]; then'
        self.assertIn(marker, source)
        functions = source.split("\n" + marker, 1)[0]
        generated = self.temp / "gateway functions under test.sh"
        generated.write_text(
            functions
            + "\n"
            + f"REPO_DIR={shlex.quote(str(repo_dir))}\n"
            + f"CONFIG_DIR={shlex.quote(str(config_dir))}\n"
            + "gateway_prerequisites() { :; }\n"
            + "install_gateway\n",
            encoding="utf-8",
        )
        return subprocess.run(
            ["/bin/sh", str(generated)],
            env=self.env(
                VPSOPS_TEST_ROOT=self.temp,
                VPSOPS_TEST_CONFIG_DIR=config_dir,
                VPSOPS_TEST_GROUP_MARKER=state_dir / "group-created",
                VPSOPS_TEST_USER_MARKER=state_dir / "user-created",
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_gateway_creates_account_on_first_run_and_preserves_config_and_keys_on_rerun(self):
        repo_dir = self.temp / "gateway repo"
        config_dir = self.temp / "gateway config"
        state_dir = self.temp / "account state"
        group_marker, user_marker = self.add_gateway_stubs(state_dir)

        first = self.run_gateway_functions(repo_dir, config_dir, state_dir)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(group_marker.is_file())
        self.assertTrue(user_marker.is_file())
        self.assertEqual(
            (config_dir / "gateway.yaml").read_text(encoding="utf-8"),
            "example gateway config\n",
        )
        self.assertEqual((config_dir / "id_ed25519").read_text(encoding="utf-8"), "generated private key\n")
        self.assertTrue((config_dir / "id_ed25519.pub").is_file())
        self.assertTrue((self.temp / "systemd" / "vpsops-gateway.service").is_file())

        retained = {
            "gateway.yaml": "operator-managed config\n",
            "tg.token": "operator token\n",
            "known_hosts": "operator host key\n",
            "id_ed25519": "operator private key\n",
            "id_ed25519.pub": "operator public key\n",
        }
        for name, contents in retained.items():
            (config_dir / name).write_text(contents, encoding="utf-8")

        second = self.run_gateway_functions(repo_dir, config_dir, state_dir)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        for name, contents in retained.items():
            with self.subTest(retained_file=name):
                self.assertEqual((config_dir / name).read_text(encoding="utf-8"), contents)

        calls = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(call.startswith("groupadd ") for call in calls), 1)
        self.assertEqual(sum(call.startswith("useradd ") for call in calls), 1)
        self.assertIn("groupadd --system vpsops", calls)
        self.assertTrue(
            any(call.startswith("useradd --system --gid vpsops ") for call in calls),
            calls,
        )
        self.assertEqual(sum(call.startswith("ssh-keygen ") for call in calls), 1)
        self.assertEqual(sum(call.startswith("git clone ") for call in calls), 1)
        self.assertIn("systemctl daemon-reload", calls)


if __name__ == "__main__":
    unittest.main()
