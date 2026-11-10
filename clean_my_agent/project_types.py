"""Project artifact rules. A directory name alone never grants cleanup permission."""
from pathlib import Path
import json
import hashlib
import os
import re
import stat

from .dependency_safety import Installation, regular_text

# Coverage follows Mole's published purge catalog; validation is implemented here.
TARGETS = frozenset(('node_modules target build dist venv .venv .pytest_cache .mypy_cache '
    '.tox .nox .ruff_cache .gradle .terragrunt-cache __pycache__ .next .nuxt .output '
    'vendor bin obj .turbo .parcel-cache .dart_tool .zig-cache zig-out .angular '
    '.svelte-kit .astro coverage DerivedData Pods .cxx .expo .build').split())
SWIFT_OUTPUTS = frozenset(('out index-build arm64-apple-macosx x86_64-apple-macosx '
                          'aarch64-unknown-linux-gnu x86_64-unknown-linux-gnu').split())
PYTHON = ('pyproject.toml', 'requirements.txt', 'setup.py', 'setup.cfg', 'Pipfile')
JVM = ('pom.xml', 'build.gradle', 'build.gradle.kts')
INDICATORS = ('package.json', 'Cargo.toml', 'go.mod', *PYTHON, *JVM, 'composer.json',
              'pubspec.yaml', 'Package.swift', 'Podfile', 'build.zig', 'build.zig.zon',
              'terragrunt.hcl', 'CMakeLists.txt', 'Makefile', 'Gemfile')
CACHE_TAG = 'Signature: 8a477f597d28d172789f06886806bc55'


def tagged(path):
    try:
        text, _ = regular_text(path / 'CACHEDIR.TAG', 4096)
        return text.splitlines()[0] == CACHE_TAG
    except (OSError, ValueError, IndexError):
        return False


def target(path):
    return path.name in TARGETS or (path.parent.name == '.build' and path.name in SWIFT_OUTPUTS) or tagged(path)


def files(parent, patterns):
    result = []
    for pattern in patterns:
        result.extend(p for p in parent.glob(pattern) if p.is_file() and not p.is_symlink())
    return sorted(set(result))


def context(path, repo):
    """Use the closest manifest boundary, including monorepo subprojects."""
    for p in (path.parent, *path.parent.parents):
        if p != repo and repo not in p.parents:
            break
        if files(p, (*INDICATORS, '*.csproj', '*.fsproj', '*.vbproj', '*.xcodeproj/project.pbxproj')):
            return p
    return repo


def describe(path, project, *, original=None):
    identity = original or path
    name = identity.name
    have = lambda *patterns: files(project, patterns)
    language, category, proof, rebuild = '', '构建产物', [], ''
    if name == 'node_modules':
        language, category, proof, rebuild = 'JavaScript / TypeScript', '项目依赖', have('package.json'), '按锁文件重新安装依赖'
    elif name in ('venv', '.venv', '.tox', '.nox', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'):
        language, proof = 'Python', have(*PYTHON)
        category = '虚拟环境' if name in ('venv', '.venv', '.tox', '.nox') else '工具缓存'
        rebuild = '按项目依赖清单重建环境' if category == '虚拟环境' else '下次运行工具时重建'
        if name in ('venv', '.venv'):
            regular_text(path / 'pyvenv.cfg', 65536)
    elif name == 'target':
        proof = have('Cargo.toml') or have('pom.xml')
        language = 'Rust' if have('Cargo.toml') else 'Java / Maven'
        rebuild = 'cargo build' if language == 'Rust' else 'mvn package'
    elif name in ('bin', 'obj'):
        proof = have('*.csproj', '*.fsproj', '*.vbproj')
        language, rebuild = '.NET', 'dotnet build'
        if name == 'bin' and not any((path / x).is_dir() for x in ('Debug', 'Release')):
            raise ValueError('bin 未发现 .NET Debug / Release 产物；保留')
    elif name == 'vendor':
        language, category, proof, rebuild = 'PHP / Composer', '项目依赖', have('composer.json'), 'composer install'
        if not have('composer.lock') or not (path / 'composer/installed.json').is_file():
            raise ValueError('仅接受有锁文件和安装记录的 Composer vendor；Go / Ruby vendor 保留')
        regular_text(project / 'composer.lock')
        json.loads(regular_text(path / 'composer/installed.json')[0])
    elif name == 'Pods':
        language, category, proof, rebuild = 'CocoaPods', '项目依赖', have('Podfile'), 'pod install'
        if not have('Podfile.lock') or not (path / 'Manifest.lock').is_file():
            raise ValueError('Pods 缺少 Podfile.lock / Manifest.lock，保留')
        if regular_text(project / 'Podfile.lock')[0] != regular_text(path / 'Manifest.lock')[0]:
            raise ValueError('Pods 安装记录与锁文件不一致，保留')
    elif name == '.gradle':
        language, category, proof, rebuild = 'Java / Gradle', '工具缓存', have(*JVM), '重新运行 Gradle 构建'
    elif name == '.terragrunt-cache':
        language, category, proof, rebuild = 'Terragrunt', '工具缓存', have('terragrunt.hcl'), '重新运行 Terragrunt'
    elif name in ('.zig-cache', 'zig-out'):
        language, proof, rebuild = 'Zig', have('build.zig', 'build.zig.zon'), 'zig build'
    elif name == '.dart_tool':
        language, proof, rebuild = 'Dart / Flutter', have('pubspec.yaml'), 'dart pub get / flutter pub get'
    elif name == 'DerivedData':
        language, proof, rebuild = 'Xcode', have('*.xcodeproj/project.pbxproj', 'Package.swift'), '重新构建 Xcode 项目'
    elif name == '.build' or (identity.parent.name == '.build' and name in SWIFT_OUTPUTS):
        language, proof, rebuild = 'SwiftPM', have('Package.swift'), 'swift build'
        if name == '.build' and any((path / p).exists() for p in ('checkouts', 'repositories', 'artifacts', 'workspace-state.json', *SWIFT_OUTPUTS)):
            raise ValueError('SwiftPM 依赖与状态保留；请选择展开的编译输出目录')
    elif name in ('.next', '.nuxt', '.output', '.turbo', '.parcel-cache', '.angular', '.svelte-kit', '.astro', '.expo'):
        language, proof, rebuild = 'JavaScript / TypeScript', have('package.json'), '重新运行对应框架构建'
    elif name == '.cxx':
        language, proof, rebuild = 'Android / CMake', have(*JVM, 'CMakeLists.txt'), '重新执行 Android / NDK 构建'
    elif name in ('build', 'dist', 'coverage'):
        choices = [('JavaScript / TypeScript', ('package.json',)), ('Python', PYTHON),
                   ('Java / Gradle', JVM), ('Dart / Flutter', ('pubspec.yaml',)),
                   ('C / C++', ('CMakeLists.txt', 'Makefile')), ('Ruby', ('Gemfile',)),
                   ('Go', ('go.mod',)), ('Rust', ('Cargo.toml',)), ('SwiftPM', ('Package.swift',))]
        for language, patterns in choices:
            proof = have(*patterns)
            if proof:
                break
        rebuild = '按项目构建或测试命令重新生成'
    elif tagged(path):
        language, category, proof, rebuild = 'CACHEDIR.TAG', '声明缓存', have(*INDICATORS), '由生成该缓存的工具重建'
    if not proof:
        raise ValueError('缺少匹配的项目清单，目录名称不足以证明可重建')
    return dict(ecosystem=language, artifact=name, artifact_category=category,
                project=str(project), evidence=[str(p) for p in proof], rebuild=rebuild)


class Evidence:
    """Preserve JS installation checks; guard non-JS artifacts against durable data."""
    def __init__(self, path, *, project=None, original=None):
        self.path = path
        self.original = original or path
        self.project = project or path.parent
        self.js = Installation(path, project=self.project) if self.original.name == 'node_modules' else None
        self.reason = None
        self.detail = None
        self.stamps = []
        if self.js:
            return
        try:
            self.detail = describe(path, self.project, original=self.original)
            for name in self.detail['evidence']:
                _, stamp = regular_text(Path(name))
                self.stamps.append((Path(name), stamp))
        except (OSError, ValueError, UnicodeError) as exc:
            self.reason = str(exc)

    def observe(self, relative, info):
        if self.js:
            self.js.observe(relative, info)
            return
        p = Path(relative)
        # Secrets, IaC state and local sources cannot be reconstructed by a build.
        private_pem = p.suffix == '.pem'
        if private_pem and stat.S_ISREG(info.st_mode):
            try:
                text = regular_text(self.path / p)[0]
                blocks = re.findall(r'-----BEGIN ([A-Z0-9 ]+)-----', text)
                private_pem = not blocks or any(b != 'CERTIFICATE' for b in blocks)
            except (OSError, ValueError, UnicodeError):
                pass
        if (private_pem or p.name == '.git' or p.name.endswith(('-keypair.json', '.tfstate', '.tfstate.backup', '.key'))
                or p.name.startswith('.env') or p.name in ('id_rsa', 'id_ed25519', 'terraform.tfstate')):
            self.reason = '包含源码仓库、凭据或持久状态；保留：' + str(p)
        if self.original.name == '__pycache__' and not stat.S_ISDIR(info.st_mode) and p.suffix not in ('.pyc', '.pyo'):
            self.reason = '__pycache__ 中含非字节码文件；保留'
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            self.reason = '包含特殊文件；保留'

    def finish(self):
        if self.js:
            return self.js.finish()
        try:
            for path, stamp in self.stamps:
                if regular_text(path)[1] != stamp:
                    self.reason = '盘点期间项目清单发生变化'
        except (OSError, ValueError):
            self.reason = '无法重新核实项目清单'
        return dict(state='unknown' if self.reason else 'verified', manager=None, marker=None,
                    reason=self.reason or '项目类型、Git 保护和生成目录规则已核对；不保证完整内容哈希一致')


def require_evidence(path, original, project):
    # A staged tree has a numeric basename; keep the original artifact identity.
    evidence = Evidence(path, project=project, original=original)
    def fail(exc):
        raise exc
    device = path.lstat().st_dev
    for base, dirs, names, fd in os.fwalk(path, follow_symlinks=False, onerror=fail):
        if '.git' in dirs or '.git' in names:
            raise ValueError('包含嵌套 Git 仓库，保留')
        for name in dirs + names:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_dev != device:
                raise ValueError('包含其他挂载卷，保留')
            evidence.observe((Path(base)/name).relative_to(path), info)
    result = evidence.finish()
    if result['state'] != 'verified':
        raise ValueError(result['reason'])


def tree_digest(path):
    """Detect additions, removals and metadata changes between staging and purge."""
    digest = hashlib.sha256()
    device = path.lstat().st_dev
    def fail(exc):
        raise exc
    for base, dirs, files, fd in os.fwalk(path, follow_symlinks=False, onerror=fail):
        dirs.sort()
        for name in sorted(dirs + files):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_dev != device:
                raise ValueError('包含其他挂载卷')
            relative = str((Path(base)/name).relative_to(path))
            record = (relative, info.st_mode, info.st_ino, info.st_size, info.st_mtime_ns)
            digest.update(repr(record).encode('utf-8', 'surrogateescape'))
    return digest.hexdigest()
