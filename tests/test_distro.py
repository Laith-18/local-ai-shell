"""Tests for distro detection and the Debian/Ubuntu rules.

ai-shell picks its rules from /etc/os-release. The `ai` fixture is pinned to Fedora and
`ai_debian` to the Debian family (see conftest.py).
"""

import pytest

OS_RELEASE = {
    "fedora": 'NAME="Fedora Linux"\nID=fedora\nVERSION_ID=44\nPRETTY_NAME="Fedora Linux 44 (KDE Plasma)"\n',
    "bazzite": 'NAME="Bazzite"\nID=bazzite\nID_LIKE="fedora"\n',
    "nobara": 'NAME="Nobara Linux"\nID=nobara\nID_LIKE="rhel centos fedora"\n',
    "debian": 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nNAME="Debian GNU/Linux"\nID=debian\n',
    "ubuntu": 'NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"\n',
    "mint": 'NAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\n',
    "pop": "NAME=\"Pop!_OS\"\nID=pop\nID_LIKE=\"ubuntu debian\"\n",
    "arch": 'NAME="Arch Linux"\nID=arch\n',
}


@pytest.mark.parametrize("name,family,shown,snap", [
    ("fedora", "fedora", "Fedora Linux", False),
    ("bazzite", "fedora", "Fedora Linux", False),
    ("nobara", "fedora", "Fedora Linux", False),
    ("debian", "debian", "Debian GNU/Linux", False),
    ("ubuntu", "debian", "Ubuntu", True),
    ("mint", "debian", "Linux Mint", True),
    ("pop", "debian", "Pop!_OS", True),
    ("arch", "fedora", "Fedora Linux", False),  # unknown distros fall back to the Fedora rules
])
def test_detect_distro(ai, name, family, shown, snap):
    d = ai.detect_distro(OS_RELEASE[name])
    assert (d.id, d.name) == (family, shown)
    assert ("snap" in d.packages) is snap


def test_override_beats_os_release(ai):
    assert ai.detect_distro(OS_RELEASE["ubuntu"], "fedora").id == "fedora"
    d = ai.detect_distro(OS_RELEASE["fedora"], "debian")
    assert (d.id, d.name) == ("debian", "Debian")


def test_os_release_quotes_and_blank_lines(ai):
    assert ai.parse_os_release("ID='ubuntu'\n\n# comment\nNAME=\"Ubuntu\"\n") == {"ID": "ubuntu", "NAME": "Ubuntu"}


# ------------------------------------------------------------------ prompts --
def router(module, distro):
    return module.render(module.ROUTER_PROMPT, distro, context="c", model="m", memories="-", facts="F")


def test_fedora_prompt_is_unchanged_in_substance(ai):
    prompt = router(ai, ai.FEDORA)
    assert "on Fedora Linux with the KDE Plasma desktop" in prompt and "sudo dnf install htop" in prompt and "dnf check-upgrade" in prompt
    assert '"update" is just another name for "upgrade"' in prompt
    assert "apt install" not in prompt


@pytest.mark.parametrize("name", ["debian", "ubuntu"])
def test_debian_family_prompt_uses_apt(ai, name):
    d = ai.detect_distro(OS_RELEASE[name])
    prompt = router(ai, d)
    assert "sudo apt install htop" in prompt and "apt list --upgradable" in prompt and "dpkg -s steam" in prompt
    assert "update only refreshes the package lists" in prompt  # the opposite of dnf
    assert "sudo dnf" not in prompt and "akmod" not in prompt and "btrfs" not in prompt
    assert ("snap" in prompt) is (name == "ubuntu")
    for template in (ai.SUMMARY_PROMPT, ai.FIX_PROMPT):
        assert d.name in ai.render(template, d, facts="F")
    assert "{" not in prompt.replace("{ ", "")


def test_session_uses_the_detected_distro(ai_debian):
    assert ai_debian.Session("m", "1m").distro.id == "debian"
    assert ai_debian.DISTRO.install_hint.format("htop") == "sudo apt install htop"


# ------------------------------------------------------------------- safety --
@pytest.mark.parametrize("cmd", ["apt list --installed", "apt search obs", "apt show htop", "apt policy steam",
                                 "apt-cache rdepends libc6", "dpkg -l", "dpkg -s steam", "dpkg -S /usr/bin/htop",
                                 "dpkg-query -W", "snap list", "snap info firefox", "ubuntu-drivers devices"])
def test_debian_queries_are_read_only(ai, ai_debian, cmd):
    assert ai.assess(cmd)[0] == "read_only"  # the classifier is the same on every distro
    assert ai_debian.assess(cmd)[0] == "read_only"


@pytest.mark.parametrize("cmd,strong", [
    ("sudo apt install htop", False), ("sudo apt update", False), ("sudo apt upgrade", False),
    ("sudo apt remove htop", True), ("sudo apt purge htop", True), ("sudo apt autoremove", True),
    ("sudo apt full-upgrade", True), ("sudo dpkg -i x.deb", True), ("sudo snap remove firefox", False),
    ("apt list -o APT::Update::Post-Invoke::=x", False), ("dpkg --configure -a", True),
])
def test_debian_changes_need_permission(ai_debian, cmd, strong):
    verdict, _, got_strong = ai_debian.assess(cmd)
    assert verdict == "permission" and got_strong is strong


def test_wrong_package_manager_is_flagged_per_distro(ai, ai_debian):
    assert any("isn't Fedora Linux's package manager" in r for r in ai.assess("sudo apt install htop")[1])
    assert not any("isn't" in r for r in ai_debian.assess("sudo apt install htop")[1])
    assert any("package manager" in r for r in ai_debian.assess("sudo dnf install htop")[1])


@pytest.mark.parametrize("cmd,fixed", [
    ("sudo apt list --upgradable", "apt list --upgradable"),
    ("sudo apt search obs", "apt search obs"),
    ("sudo apt-cache policy steam", "apt-cache policy steam"),
    ("sudo dpkg -l | grep steam", "dpkg -l | grep steam"),
    ("sudo snap list", "snap list"),
])
def test_sudo_is_dropped_from_debian_queries(ai_debian, cmd, fixed):
    assert ai_debian.normalise_command(cmd)[0] == fixed


@pytest.mark.parametrize("cmd", ["sudo apt install htop", "sudo apt update", "sudo dpkg -i x.deb",
                                 "sudo snap install discord"])
def test_sudo_is_kept_for_debian_changes(ai_debian, cmd):
    assert ai_debian.normalise_command(cmd) == (cmd, [])


# --------------------------------------------------------------------- eval --
@pytest.fixture(scope="module")
def ev():
    import importlib.util

    from conftest import ROOT
    spec = importlib.util.spec_from_file_location("run_eval_distro", ROOT / "eval" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_task_sets_per_distro(ev):
    fedora = {t["id"] for t in ev.load_tasks(distro="fedora")}
    debian = {t["id"] for t in ev.load_tasks(distro="debian")}
    assert {"install-htop", "fedora-version", "dnf-update-vs-upgrade"} <= fedora - debian
    assert {"apt-install-htop", "debian-version", "apt-update-vs-upgrade"} <= debian - fedora
    assert {"disk-free", "sunshine-restart", "append-bashrc", "danger-wipe-home"} <= fedora & debian


@pytest.mark.parametrize("distro", ["fedora", "debian"])
def test_every_reference_answer_passes_for_its_distro(ai, ev, distro):
    for task in ev.load_tasks(distro=distro):
        reply = task["example"] if task["kind"] == "answer" else f"$ {task['example']}"
        result = ev.grade(ai, task, reply)
        assert result["passed"] and not result["tier_mismatch"], (distro, task["id"], result)


def test_debian_eval_prompt_uses_debian_rules_and_facts(ai, ev):
    task = next(t for t in ev.load_tasks(distro="debian") if t["id"] == "apt-install-htop")
    system = ev.build_messages(ai, "m", task, distro="debian")[0]["content"]
    assert "on Debian" in system and "sudo apt install htop" in system and "debian-box" in system
    assert "550.163.01" in system and "sudo dnf" not in system


def test_saying_apt_update_and_upgrade_are_the_same_fails(ai, ev):
    task = next(t for t in ev.load_tasks(distro="debian") if t["id"] == "apt-update-vs-upgrade")
    assert not ev.grade(ai, task, "They're the same: update is just another name for upgrade.")["passed"]
    assert ev.grade(ai, task, "update refreshes the package lists; upgrade installs the new versions.")["passed"]


def test_unknown_distro_in_tasks_is_rejected(ev, tmp_path):
    bad = tmp_path / "t.toml"
    bad.write_text('[[task]]\nid = "x"\ncategory = "c"\nkind = "answer"\nprompt = "p"\ndistros = ["arch"]\n')
    with pytest.raises(ValueError, match="distros"):
        ev.load_tasks(bad)
