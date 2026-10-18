#!/usr/bin/env python3
"""
cleanup.py

Automatically removes deleted proprietary blobs from vendor tree (physical files,
<device>-vendor.mk, and Android.bp) based on changes in proprietary-files.txt.
"""

import sys
import os
import re
import argparse
import subprocess
from pathlib import Path


def parse_proprietary_diff_line(line: str) -> tuple:
    """Parses a diff line from proprietary-files.txt and extracts file paths, module names, and symlinks."""
    raw = line.strip()
    if not raw or raw.startswith('#'):
        return set(), set()
    if raw.startswith('-'):
        raw = raw[1:].strip()

    raw = raw.split('#')[0].strip()
    parts = raw.split('|')[0].strip()
    flag_parts = parts.split(';')
    file_spec = flag_parts[0].strip()

    file_paths = set()
    module_names = set()

    if ':' in file_spec:
        src, dest = file_spec.split(':', 1)
        file_paths.add(src.strip())
        file_paths.add(dest.strip())
    else:
        file_paths.add(file_spec)

    for fp in file_paths:
        base = os.path.basename(fp)
        module_names.add(base)
        for ext in ('.so', '.apk', '.jar', '.xml'):
            if base.endswith(ext):
                module_names.add(base[:-len(ext)])

    for part in flag_parts[1:]:
        if part.startswith('SYMLINK='):
            for s in part[len('SYMLINK='):].split(':'):
                s = s.strip()
                if s:
                    module_names.add(s.replace('/', '_').replace('.', '_'))

    return file_paths, module_names


def get_deleted_files_and_modules(device_dir: Path, rev: str = None) -> tuple:
    """Gets deleted proprietary file paths and candidate module names from git diff."""
    cmd = ['git', '-C', str(device_dir), 'diff']
    if rev:
        if '..' in rev or '~' in rev or '^' in rev:
            cmd.extend([rev, '--', 'proprietary-files.txt'])
        else:
            cmd.extend([f'{rev}^..{rev}', '--', 'proprietary-files.txt'])
    else:
        cmd.extend(['--', 'proprietary-files.txt'])

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        cmd = ['git', '-C', str(device_dir), 'diff', 'HEAD~1..HEAD', '--', 'proprietary-files.txt']
        res = subprocess.run(cmd, capture_output=True, text=True)

    all_file_paths = set()
    all_module_names = set()

    for line in res.stdout.splitlines():
        if line.startswith('-') and not line.startswith('---'):
            fps, mods = parse_proprietary_diff_line(line)
            for fp in fps:
                if fp.strip():
                    all_file_paths.add(fp.strip())
            for mod in mods:
                if mod.strip():
                    all_module_names.add(mod.strip())

    return sorted(list(all_file_paths)), all_module_names


def remove_block_from_android_bp(bp_path: Path, module_names: set, file_paths: set) -> int:
    """Removes prebuilt module blocks from Android.bp that match module_name or file_path."""
    if not bp_path.exists():
        return 0

    content = bp_path.read_text()

    block_pattern = re.compile(
        r'([a-zA-Z0-9_]+)\s*\{\s*\n(.*?)\n\}\n*',
        re.DOTALL
    )

    removed_count = 0

    def should_remove_block(block_type: str, block_body: str) -> bool:
        name_match = re.search(r'name:\s*"([^"]+)"', block_body)
        if name_match:
            name = name_match.group(1)
            if name in module_names:
                return True

        for fp in file_paths:
            if f'proprietary/{fp}' in block_body:
                return True
        return False

    new_blocks = []
    last_idx = 0
    for match in block_pattern.finditer(content):
        b_type = match.group(1)
        b_body = match.group(2)

        if should_remove_block(b_type, b_body):
            new_blocks.append(content[last_idx:match.start()])
            last_idx = match.end()
            removed_count += 1
        else:
            new_blocks.append(content[last_idx:match.end()])
            last_idx = match.end()

    new_blocks.append(content[last_idx:])
    new_content = "".join(new_blocks)

    new_content = re.sub(r'\n{3,}', '\n\n', new_content)
    bp_path.write_text(new_content)
    return removed_count


def remove_entries_from_vendor_mk(mk_path: Path, file_paths: set, module_names: set) -> tuple:
    """Removes file copies and packages from <device>-vendor.mk with correct backslash continuation."""
    if not mk_path.exists():
        return 0, 0

    lines = mk_path.read_text().splitlines(keepends=True)
    new_lines = []
    i = 0
    n = len(lines)
    copy_removed = 0
    pkg_removed = 0

    while i < n:
        line = lines[i]
        stripped = line.strip()

        is_copy_files = stripped.startswith('PRODUCT_COPY_FILES +=') or stripped.startswith('PRODUCT_COPY_FILES :=')
        is_packages = stripped.startswith('PRODUCT_PACKAGES +=') or stripped.startswith('PRODUCT_PACKAGES :=')
        is_boot_jars = stripped.startswith('PRODUCT_BOOT_JARS +=') or stripped.startswith('PRODUCT_BOOT_JARS :=')

        if is_copy_files or is_packages or is_boot_jars:
            sec_header = line
            sec_items = []
            i += 1
            while i < n:
                item_line = lines[i]
                item_stripped = item_line.strip()
                if not item_stripped:
                    break
                has_continuation = item_stripped.endswith('\\')
                val = item_stripped[:-1].strip() if has_continuation else item_stripped.strip()

                matched = False
                if is_copy_files:
                    for fp in file_paths:
                        if f'proprietary/{fp}:' in val:
                            matched = True
                            copy_removed += 1
                            break
                elif is_packages or is_boot_jars:
                    if val in module_names:
                        matched = True
                        pkg_removed += 1

                if not matched:
                    sec_items.append(val)

                if not has_continuation:
                    i += 1
                    break
                i += 1

            if sec_items:
                new_lines.append(sec_header)
                for idx, itm in enumerate(sec_items):
                    if idx < len(sec_items) - 1:
                        new_lines.append(f'    {itm} \\\n')
                    else:
                        new_lines.append(f'    {itm}\n')
                new_lines.append('\n')
        else:
            new_lines.append(line)
            i += 1

    mk_path.write_text("".join(new_lines))
    return copy_removed, pkg_removed


def main():
    parser = argparse.ArgumentParser(description="Clean up vendor tree from deleted proprietary-files.")
    parser.add_argument("--device-dir", "-d", default=".", help="Path to device tree directory")
    parser.add_argument("--vendor-dir", "-v", default=None, help="Path to vendor directory (auto-detected if omitted)")
    parser.add_argument("--commit", "-c", default=None, help="Git commit or diff range in device tree")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Print what would be deleted without modifying files")
    args = parser.parse_args()

    device_dir = Path(args.device_dir).resolve()

    vendor_dir = args.vendor_dir
    if not vendor_dir:
        parts = device_dir.parts
        if 'device' in parts:
            idx = parts.index('device')
            rel_parts = parts[idx+1:]
            candidate = Path(*parts[:idx]) / 'vendor' / Path(*rel_parts)
            if candidate.exists():
                vendor_dir = candidate

    if not vendor_dir or not Path(vendor_dir).exists():
        print(f"Error: Could not locate vendor directory: {vendor_dir}", file=sys.stderr)
        sys.exit(1)

    vendor_dir = Path(vendor_dir).resolve()
    print(f"Device Directory: {device_dir}")
    print(f"Vendor Directory: {vendor_dir}")

    deleted_files, module_names = get_deleted_files_and_modules(device_dir, args.commit)
    if not deleted_files:
        print("No deleted files detected in proprietary-files.txt.")
        return

    print(f"\nFound {len(deleted_files)} deleted file(s) from proprietary-files.txt:")
    for f in deleted_files:
        print(f"  - {f}")

    if args.dry_run:
        print(f"\nIdentified {len(module_names)} candidate module name(s) to remove.")
        print("Dry run completed. No changes made.")
        return

    # 1. Remove physical files
    file_paths_set = set(deleted_files)
    removed_files = 0
    for fp in deleted_files:
        if not fp:
            continue
        full_path = vendor_dir / 'proprietary' / fp
        if full_path.is_file() or full_path.is_symlink():
            full_path.unlink()
            removed_files += 1
            parent = full_path.parent
            while parent != vendor_dir / 'proprietary' and parent.exists():
                try:
                    parent.rmdir()
                    parent = parent.parent
                except OSError:
                    break

    print(f"\n[1] Removed {removed_files} physical file(s) from {vendor_dir / 'proprietary'}")

    # 2. Update <device>-vendor.mk
    mk_files = list(vendor_dir.glob('*-vendor.mk'))
    for mk in mk_files:
        copies, pkgs = remove_entries_from_vendor_mk(mk, file_paths_set, module_names)
        print(f"[2] Updated {mk.name}: removed {copies} COPY rule(s), {pkgs} PACKAGE/BOOT_JAR entry(ies)")

    # 3. Update Android.bp
    bp_path = vendor_dir / 'Android.bp'
    if bp_path.exists():
        bp_removed = remove_block_from_android_bp(bp_path, module_names, file_paths_set)
        print(f"[3] Updated Android.bp: removed {bp_removed} module block(s)")

    print("\nVendor cleanup completed successfully!")


if __name__ == '__main__':
    main()
