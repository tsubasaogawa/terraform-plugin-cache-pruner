"""
Parse .terraform.lock.hcl files to determine used provider/version pairs,
compare with Terraform plugin cache, and produce (or perform) cleanup
actions: list, move-to-backup, or delete. Default is dry-run.

Usage examples:
  python main.py \
    --repo /path/to/terraform/dir \
    --cache-dir "$HOME/.terraform.d/plugin_cache/registry.terraform.io" \
    --dry-run

  # Backup and actually remove unused versions:
  python main.py \
    --repo /path/to/terraform/dir \
    --cache-dir "$HOME/.terraform.d/plugin_cache/registry.terraform.io" \
    --backup /tmp/plugin_cache_backup.tgz --execute --remove-empty-root
"""

import argparse
import os
import re
import shutil
import tarfile
from pathlib import Path
from typing import Dict, Set, Tuple

PROVIDER_RE = re.compile(r'^provider\s+"([^"]+)"')
VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"')
# matches filenames like: terraform-provider-aws_v2.30.0_x4
ARCH_FILE_RE = re.compile(r'^terraform-provider-([^_]+)_v([^_]+)_x')


def parse_lockfile(path: Path) -> Set[str]:
    """Return set of provider/version strings like 'namespace/provider/version'.
    If version can't be found for a provider block, include 'namespace/provider' only.
    """
    results = set()
    try:
        with path.open('r', encoding='utf-8') as f:
            current = None
            for line in f:
                m = PROVIDER_RE.match(line)
                if m:
                    src = m.group(1)
                    # strip registry prefix if present
                    if src.startswith('registry.terraform.io/'):
                        src = src.split('registry.terraform.io/', 1)[1]
                    current = src
                    continue
                if current:
                    mv = VERSION_RE.match(line)
                    if mv:
                        ver = mv.group(1)
                        results.add(f"{current}/{ver}")
                        current = None
                    elif line.strip().startswith('}'):
                        # end of block without version
                        results.add(current)
                        current = None
    except Exception:
        pass
    return results


def gather_used_providers(repo: Path) -> Set[str]:
    used = set()
    for root, _, files in os.walk(repo):
        for fn in files:
            if fn == '.terraform.lock.hcl' or fn == 'terraform.lock.hcl':
                path = Path(root) / fn
                used |= parse_lockfile(path)
    return used


def list_cache_entries(cache_registry_dir: Path) -> Tuple[Dict[str, Set[str]], Set[str]]:
    """Return (mapping provider -> set(versions)), and set of entries (provider and provider/version).
    provider is 'namespace/provider'
    """
    provider_map = {}
    all_entries = set()
    if not cache_registry_dir.exists():
        return provider_map, all_entries
    # iterate namespace/provider/version
    for namespace_dir in cache_registry_dir.iterdir():
        if not namespace_dir.is_dir():
            continue
        ns = namespace_dir.name
        for provider_dir in namespace_dir.iterdir():
            if not provider_dir.is_dir():
                continue
            provider = f"{ns}/{provider_dir.name}"
            # include provider root entry
            all_entries.add(provider)
            versions = set()
            for child in provider_dir.iterdir():
                if child.is_dir():
                    versions.add(child.name)
                    all_entries.add(f"{provider}/{child.name}")
            provider_map[provider] = versions
    return provider_map, all_entries


def list_arch_cache_entries(arch_dir: Path):
    """Scan linux_amd64 directory and return list of (provider_short, version, path) and list of skipped filenames"""
    entries = []
    skipped = []
    if not arch_dir.exists() or not arch_dir.is_dir():
        return entries, skipped
    for child in arch_dir.iterdir():
        # only files
        if not child.is_file():
            continue
        m = ARCH_FILE_RE.match(child.name)
        if not m:
            skipped.append(child.name)
            continue
        provider_short = m.group(1)
        version = m.group(2)
        entries.append((provider_short, version, child))
    return entries, skipped


def make_backup(cache_dir: Path, backup_path: Path) -> None:
    with tarfile.open(backup_path, 'w:gz') as tf:
        tf.add(str(cache_dir), arcname=os.path.basename(str(cache_dir)))


def main():
    p = argparse.ArgumentParser(description='Prune Terraform plugin_cache by comparing lockfiles')
    p.add_argument('--repo', required=False, default=os.getcwd(), help='Path to terraform repo root')
    p.add_argument('--cache-dir', required=False,
                   default=str(Path.home() / '.terraform.d' / 'plugin_cache' / 'registry.terraform.io'),
                   help='Path to registry.terraform.io inside plugin_cache')
    # prune-linux-amd64 is enabled by default; BooleanOptionalAction creates --no-prune-linux-amd64
    p.add_argument('--prune-linux-amd64', action=argparse.BooleanOptionalAction, default=True,
                   help='Prune files under plugin_cache/linux_amd64 (default: enabled)')
    p.add_argument('--backup', required=False, help='Create tar.gz backup before deleting (path)')
    p.add_argument('--execute', action='store_true', help='Actually perform deletions/moves. Default: dry-run')
    p.add_argument('--move-to', required=False, help='Move candidates to this directory instead of deleting')
    p.add_argument('--remove-empty-root', action='store_true', help='Remove provider root dirs when empty after version deletions')
    p.add_argument('--log', required=False, default='/tmp/terraform_plugin_cache_prune.log', help='Log file path')
    args = p.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    cache_registry_dir = Path(args.cache_dir).expanduser().resolve()
    log_path = Path(args.log)

    used = gather_used_providers(repo)
    provider_map, all_cache_entries = list_cache_entries(cache_registry_dir)

    # optionally scan linux_amd64
    arch_entries = []
    arch_skipped = []
    arch_dir = cache_registry_dir.parent / 'linux_amd64'
    if args.prune_linux_amd64:
        arch_entries, arch_skipped = list_arch_cache_entries(arch_dir)

    # Build set of used provider/version entries and used providers
    used_versions = set()
    used_providers = set()
    for item in used:
        if '/' in item:
            # item could be databricks/databricks/1.90.0 or hashicorp/aws/5.17.0
            used_versions.add(item)
            # provider = namespace/provider
            parts = item.split('/')
            if len(parts) >= 2:
                used_providers.add(f"{parts[0]}/{parts[1]}")
        else:
            used_providers.add(item)

    # Helper to check if an arch file (provider_short,version) is referenced in used_versions
    def _arch_is_used(provider_short: str, version: str) -> bool:
        # match cases like 'hashicorp/aws/1.2.3' or 'aws/1.2.3'
        target_suffix = f"/{provider_short}/{version}"
        if f"{provider_short}/{version}" in used_versions:
            return True
        for uv in used_versions:
            if uv.endswith(target_suffix):
                return True
        return False

    # Determine candidate version directories to remove
    to_remove_versions = []
    for provider, versions in provider_map.items():
        for v in versions:
            entry = f"{provider}/{v}"
            if entry not in used_versions:
                to_remove_versions.append(entry)

    # Determine provider roots that may be removed if empty
    empty_roots = []
    for provider, versions in provider_map.items():
        remaining = [v for v in versions if f"{provider}/{v}" in used_versions]
        if not remaining:
            empty_roots.append(provider)

    # Determine arch (linux_amd64) files to remove
    to_remove_arch_files = []
    if args.prune_linux_amd64:
        for provider_short, version, path in arch_entries:
            if not _arch_is_used(provider_short, version):
                to_remove_arch_files.append(path)

    # Logging and output
    with log_path.open('w', encoding='utf-8') as logf:
        logf.write(f"repo={repo}\n")
        logf.write(f"cache_registry_dir={cache_registry_dir}\n")
        logf.write(f"used_versions_count={len(used_versions)}\n")
        logf.write(f"providers_in_cache={len(provider_map)}\n")
        logf.write(f"version_dirs_total={sum(len(s) for s in provider_map.values())}\n")
        logf.write(f"candidates_versions_to_remove={len(to_remove_versions)}\n")
        # arch info
        if args.prune_linux_amd64:
            logf.write(f"linux_amd64_dir={arch_dir}\n")
            logf.write(f"linux_amd64_total_files={len(arch_entries)+len(arch_skipped)}\n")
            logf.write(f"linux_amd64_skipped_files={len(arch_skipped)}\n")
            logf.write(f"candidates_arch_files_to_remove={len(to_remove_arch_files)}\n")
        logf.write('\n')
        logf.write('--- candidates (versions) ---\n')
        for e in to_remove_versions:
            logf.write(f"{e}\n")
        logf.write('\n')
        logf.write('--- possible provider roots to remove (if requested) ---\n')
        for e in empty_roots:
            logf.write(f"{e}\n")
        if args.prune_linux_amd64:
            logf.write('\n')
            logf.write('--- candidates (linux_amd64 files) ---\n')
            for p in to_remove_arch_files:
                logf.write(f"{p}\n")
            if arch_skipped:
                logf.write('\n')
                logf.write('--- skipped linux_amd64 filenames (unparsed) ---\n')
                for s in arch_skipped:
                    logf.write(f"{s}\n")

    print(f"Found {len(used_versions)} used provider/version entries")
    print(f"Cache providers: {len(provider_map)}, version dirs total: {sum(len(s) for s in provider_map.values())}")
    print(f"Candidates to remove (versions): {len(to_remove_versions)}")
    print(f"Provider roots potentially removable: {len(empty_roots)}")
    print(f"Log written to: {log_path}")

    if not args.execute:
        print('\nDry-run mode. Re-run with --execute to perform deletions or --move-to to move candidates.')
        return

    # If backup requested, create backup
    if args.backup:
        backup_path = Path(args.backup).expanduser().resolve()
        print(f"Backing up {cache_registry_dir.parent} -> {backup_path}")
        make_backup(cache_registry_dir.parent, backup_path)
        print('Backup complete')

    # If move-to specified, ensure dir exists
    move_to = Path(args.move_to).expanduser().resolve() if args.move_to else None
    if move_to:
        move_to.mkdir(parents=True, exist_ok=True)

    # Perform deletions/moves for version dirs
    removed_count = 0
    moved_count = 0
    failed = 0
    for entry in to_remove_versions:
        target = cache_registry_dir.parent / entry  # because cache_registry_dir is .../plugin_cache/registry.terraform.io
        if not target.exists():
            # skip missing
            continue
        try:
            if move_to:
                dest = move_to / entry
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(dest))
                moved_count += 1
                print(f"MOVED: {entry}")
            else:
                # delete
                if target.is_dir():
                    shutil.rmtree(str(target))
                else:
                    target.unlink()
                removed_count += 1
                print(f"REMOVED: {entry}")
        except Exception as ex:
            print(f"FAILED: {entry}: {ex}")
            failed += 1

    # Perform deletions/moves for linux_amd64 files
    for fpath in to_remove_arch_files:
        try:
            if move_to:
                dest = move_to / 'linux_amd64' / fpath.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(fpath), str(dest))
                moved_count += 1
                print(f"MOVED ARCH: {fpath.name}")
            else:
                fpath.unlink()
                removed_count += 1
                print(f"REMOVED ARCH: {fpath.name}")
        except Exception as ex:
            print(f"FAILED ARCH: {fpath}: {ex}")
            failed += 1

    # Optionally remove empty provider roots
    removed_roots = 0
    if args.remove_empty_root:
        for provider in empty_roots:
            root = cache_registry_dir.parent / provider
            try:
                if root.exists() and root.is_dir():
                    # remove only if empty
                    if not any(root.iterdir()):
                        shutil.rmtree(str(root))
                        removed_roots += 1
                        print(f"REMOVED ROOT: {provider}")
            except Exception as ex:
                print(f"FAILED ROOT REMOVE: {provider}: {ex}")

    print(f"Done. removed={removed_count}, moved={moved_count}, failed={failed}, removed_roots={removed_roots}")


if __name__ == '__main__':
    main()
