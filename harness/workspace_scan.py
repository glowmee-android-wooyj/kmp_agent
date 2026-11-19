"""Pruned observation walks; approved source fingerprints remain independent."""
import os
from pathlib import Path

GENERATED = {'build', 'node_modules', '.gradle', '.kotlin', 'dist'}


def paths(root, *, prune_generated=True):
    root = Path(root)
    protected = None
    inspected = False

    def generated_only(relative):
        nonlocal protected, inspected
        if not inspected:
            from harness.tracking_io import run_git
            result = run_git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
            inspected = True
            if result.returncode == 0:
                protected = set()
                for name in result.stdout.decode().split('\0'):
                    if name:
                        item = Path(name)
                        protected.add(item.as_posix())
                        protected.update(parent.as_posix() for parent in item.parents)
        # A failed Git inspection never becomes permission to omit source.
        return protected is not None and relative.as_posix() not in protected

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
