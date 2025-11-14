# Copyright 2023 The Tensorflow Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tool to rearrange files and build the wheel.

In a nutshell this script does:
1) Takes lists of paths to .h/.py/.so/etc files.
2) Creates a temporary directory.
3) Copies files from #1 to #2 with some exceptions and corrections.
4) A wheel is created from the files in the temp directory.

Most of the corrections are related to tsl/xla vendoring:
These files used to be a part of source code but were moved to an external repo.
To not break the TF API, we pretend that it's still part of the it.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import os
import shutil
import sys


def is_windows() -> bool:
  return sys.platform.startswith("win32")


def is_macos() -> bool:
  return sys.platform.startswith("darwin")


def copy_file(
    src_file: str,
    dst_dir: str,
    strip: str = None,
    dest_file: str = None,
) -> None:
  """Copy a file to the destination directory.

  Args:
    src_file: file to be copied
    dst_dir: destination directory
    strip: prefix to strip before copying to destination
    dest_file: destanation file location if different from src_file
  """

  # strip `bazel-out/.../bin/` for generated files.
  dest = dest_file if dest_file else src_file
  if dest.startswith("bazel-out"):
    dest = dest[dest.index("bin") + 4 :]

  if strip:
    dest = dest.removeprefix(strip)
  dest_dir_path = os.path.join(dst_dir, os.path.dirname(dest))
  os.makedirs(dest_dir_path, exist_ok=True)
  shutil.copy(src_file, dest_dir_path)
  os.chmod(os.path.join(dst_dir, dest), 0o644)


def parse_args() -> argparse.Namespace:
  """Arguments parser."""
  parser = argparse.ArgumentParser(
      description="Helper for building pip package", fromfile_prefix_chars="@"
  )
  parser.add_argument(
      "--output-name",
      required=True,
      help="Output file for the wheel, mandatory",
  )
  parser.add_argument(
      "--project-name",
      required=True,
      help="Project name to be passed to setup.py",
  )
  parser.add_argument(
      "--platform",
      required=True,
      help="Platform name to be passed to setup.py",
  )
  parser.add_argument(
      "--headers", help="header files for the wheel", action="append"
  )
  parser.add_argument(
      "--srcs", help="source files for the wheel", action="append"
  )
  parser.add_argument("--dests", help="", action="append", default=[])
  parser.add_argument("--version", help="Reverb version")
  return parser.parse_args()


def prepare_srcs(
    deps: list[str], deps_destinations: list[str], srcs_dir: str
) -> None:
  """Rearrange source files in target the target directory.

  Exclude `external` files and move vendored xla/tsl files accordingly.

  Args:
    deps: a list of paths to files.
    deps_destinations: a list of json files with mapping of deps to their
      destinations for deps whose original path and path inside the wheel are
      different.
    srcs_dir: target directory where files are copied to.
  """

  deps_mapping_dict = {}
  for deps_destination in deps_destinations:
    with open(deps_destination, "r") as deps_destination_file:
      deps_mapping_dict.update(json.load(deps_destination_file))

  for file in deps:
    # exclude external py files
    if "external" not in file:
      if file in deps_mapping_dict:
        dest = deps_mapping_dict[file]
        if dest:
          copy_file(file, srcs_dir, None, dest)
      else:
        copy_file(file, srcs_dir, None, None)


def prepare_wheel_srcs(
    headers: list[str],
    srcs: list[str],
    dests: list[str],
    srcs_dir: str,
) -> None:
  """Rearrange source and header files.

  Args:
    headers: a list of paths to header files.
    srcs: a list of paths to the rest of files.
    dests: a list of paths to files with srcs files destinations.
    aot: a list of paths to files that should be in xla_aot directory.
    srcs_dir: directory to copy files to.
    version: tensorflow version.
  """
  prepare_srcs(srcs, dests, srcs_dir)

  # move MANIFEST and THIRD_PARTY_NOTICES to the root
  shutil.move(
    os.path.join(srcs_dir, "reverb/pip_package/MANIFEST.in"),
    os.path.join(srcs_dir, "MANIFEST.in"),
  )

  shutil.move(
    os.path.join(srcs_dir, "reverb/pip_package/setup.py"),
    os.path.join(srcs_dir, "setup.py"),
  )

  # Means the wheel is built with pywrap rules
  if dests:
    return

  if not is_macos() and not is_windows():
    patch_so(srcs_dir)


def patch_so(srcs_dir: str) -> None:
  """Patch .so files.

  We must patch some of .so files otherwise auditwheel will fail.

  Args:
    srcs_dir: target directory with .so files to patch.
  """
  to_patch = {
      "reverb/libpybind.so": "$ORIGIN/../tensorflow",
      "reverb/cc/ops/libgen_reverb_ops_gen_op.so": "$ORIGIN/../../../tensorflow",
  }
  for file, path in to_patch.items():
    rpath = (
        subprocess.check_output(
            ["patchelf", "--print-rpath", "{}/{}".format(srcs_dir, file)]
        )
        .decode()
        .strip()
    )
    new_rpath = rpath + ":" + path
    subprocess.run(
        ["patchelf", "--set-rpath", new_rpath, "{}/{}".format(srcs_dir, file)],
        check=True,
    )
    subprocess.run(
        ["patchelf", "--shrink-rpath", "{}/{}".format(srcs_dir, file)],
        check=True,
    )


def build_wheel(
    dir_path: str,
    cwd: str,
    project_name: str,
    platform: str,
    version: str,
) -> None:
  """Build the wheel in the target directory.

  Args:
    dir_path: directory where the wheel will be stored
    cwd: path to directory with wheel source files
    project_name: name to pass to setup.py.
    platform: platform name to pass to setup.py.
    collab: defines if this is a collab build
  """
  env = os.environ.copy()
  env['version'] = version
  subprocess.run(
      [
          sys.executable,
          "setup.py",
          "bdist_wheel",
          "--release",
          f"--dist-dir={dir_path}",
          f"--plat-name={platform}",
      ],
      check=True,
      cwd=cwd,
      env=env
  )


if __name__ == "__main__":
  args = parse_args()
  temp_dir = tempfile.TemporaryDirectory(prefix="reverb_wheel")
  temp_dir_path = temp_dir.name
  try:
    prepare_wheel_srcs(
        args.headers,
        args.srcs,
        args.dests,
        temp_dir_path,
    )
    build_wheel(
        os.path.join(os.getcwd(), args.output_name),
        temp_dir_path,
        args.project_name,
        args.platform,
        args.version
    )
  finally:
    temp_dir.cleanup()