"""Pruned observation walks; approved source fingerprints remain independent."""
import os
from pathlib import Path

GENERATED = {'build', 'node_modules', '.gradle', '.kotlin', 'dist'}
ROOT_BUILD_CACHES = {'.gradle-local', '.konan-local', '.xcode-derived'}


def protected_git_paths(root):
    from harness.tracking_io import run_git
    result = run_git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
    if result.returncode:
        return None
    protected = set()
    for name in result.stdout.decode().split('\0'):
        if name:
            item = Path(name)
            protected.add(item.as_posix())
            protected.update(parent.as_posix() for parent in item.parents)
    return protected


def generated_directory(relative, protected, *, include_root_build_caches=False):
    eligible = relative.name in GENERATED or relative.parts == ('tools', '.runtime')
    if include_root_build_caches and len(relative.parts) == 1 and relative.name in ROOT_BUILD_CACHES:
        eligible = True
    return eligible and protected is not None and relative.as_posix() not in protected


def paths(root, *, prune_generated=True, prune_root_build_caches=False):
    root = Path(root)
    protected = None
    inspected = False

    def generated_only(relative):
        nonlocal protected, inspected
        if not inspected:
            protected = protected_git_paths(root)
            inspected = True
        # A failed Git inspection never becomes permission to omit source.
        return generated_directory(relative, protected,
                                   include_root_build_caches=prune_root_build_caches)

    def failed(error):
        raise error

    from harness.tracking_io import check_deadline
    for folder, directories, files in os.walk(root, topdown=True, followlinks=False, onerror=failed):
        check_deadline()
        parent = Path(folder)
        kept = []
        for name in sorted(directories):
            path = parent / name
            relative = path.relative_to(root)
            if (name in {'.git', '.harness', '__pycache__'} or relative.parts == ('verification',)
                    or relative.parts == ('.kmp-agent', 'releases')):
                continue
            if path.is_symlink():
                yield path
                continue
            candidate = name in GENERATED or relative.parts == ('tools', '.runtime')
            if prune_root_build_caches and len(relative.parts) == 1 and name in ROOT_BUILD_CACHES:
                candidate = True
            if prune_generated and candidate and generated_only(relative):
                continue
            kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            check_deadline()
            path = parent / name
            relative = path.relative_to(root)
            if (name not in {'.DS_Store', '.git', '.harness', '__pycache__'}
                    and relative.parts != ('verification',)
                    and relative.parts != ('.kmp-agent', 'releases')):
                yield path
