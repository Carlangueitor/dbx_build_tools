# mypy: allow-untyped-defs, allow-untyped-globals

from __future__ import print_function

import os

from collections import defaultdict

import build_tools.bzl_lib.metrics as metrics

from build_tools import bazel_utils
from build_tools.bzl_lib import build_merge
from build_tools.bzl_lib.run import run_cmd

from dropbox import runfiles

HEADER = (
    "# @"
    + """generated: This file was generated by bzl. Do not modify!
# Argument overrides and custom targets should be specified in BUILD.in.

"""
)


class CopyGenerator(object):
    """This creates empty BUILD.gen_empty files to ensure BUILD.in contents are
    copied into BUILD, even when it does not include any generated targets"""

    def __init__(
        self,
        workspace_dir,
        generated_files,
        verbose,
        skip_deps_generation,
        dry_run,
        use_magic_mirror,
    ):

        self.workspace_dir = workspace_dir
        self.generated_files = generated_files
        self.dry_run = dry_run

        self.visited_dirs = set()

    def regenerate(self, bazel_targets, cwd="."):
        targets = bazel_utils.expand_bazel_target_dirs(
            self.workspace_dir,
            [t for t in bazel_targets if not t.startswith("@")],
            require_build_file=False,
            cwd=cwd,
        )

        for target in targets:
            assert target.startswith("//"), "Target must be absolute: " + target
            pkg, _, _ = target.partition(":")
            target_dir = os.path.join(self.workspace_dir, pkg[2:])

            if target_dir in self.visited_dirs:
                continue
            self.visited_dirs.add(target_dir)

            if not os.path.exists(os.path.join(target_dir, "BUILD.in")):
                continue

            if target_dir in self.generated_files:
                continue

            if self.dry_run:
                continue

            out = os.path.join(target_dir, "BUILD.gen_empty")
            open(out, "w").close()

            self.generated_files[target_dir].append(out)


options = None


class GazelError(Exception):
    pass


def regenerate_build_files(
    bazel_targets,
    generators,
    verbose=False,
    skip_deps_generation=False,
    dry_run=False,
    reverse_deps_generation=False,
    use_magic_mirror=False,
):
    workspace_dir = bazel_utils.find_workspace()

    if reverse_deps_generation:
        targets = bazel_utils.expand_bazel_target_dirs(
            workspace_dir,
            [t for t in bazel_targets if not t.startswith("@")],
            require_build_file=False,
            cwd=".",
        )
        pkgs = [t.partition(":")[0] for t in targets]

        patterns = ['"%s"' % pkg for pkg in pkgs]
        patterns.extend(['"%s:' % pkg for pkg in pkgs])

        bazel_targets = set(bazel_targets)
        for path, dirs, files in os.walk(workspace_dir):
            if "BUILD" not in files:
                continue

            build_content = open(os.path.join(workspace_dir, path, "BUILD")).read()

            should_regen = False
            for pattern in patterns:
                if pattern in build_content:
                    should_regen = True
                    break

            if should_regen:
                # convert abs path to relative to workspace
                bazel_targets.add("//" + os.path.relpath(path, workspace_dir))

    generated_files = defaultdict(list)  # type: ignore[var-annotated]

    generator_instances = [
        generator(
            workspace_dir,
            generated_files,
            verbose,
            skip_deps_generation,
            dry_run,
            use_magic_mirror,
        )
        for generator in generators
    ]

    # In order to ensure we don't miss generating specific target types,
    # recursively expands the generated set until it converges.
    prev_visited_dirs = set()  # type: ignore[var-annotated]
    updated_pkgs = set()  # type: ignore[var-annotated]

    while bazel_targets:
        for generator in generator_instances:
            with metrics.generator_metric_context(generator.__class__.__name__):
                res = generator.regenerate(bazel_targets)
            # Generators are expected to do one/both of
            # - return a list of packages/directores where it could have modified BUILD files
            # - Update self.generated_files mapping for BUILD path -> BUILD file fragments
            if res:
                updated_pkgs.update(res)

        visited_dirs = set(generated_files.keys())
        newly_visited_dirs = visited_dirs.difference(prev_visited_dirs)
        if newly_visited_dirs:
            # continue processing
            prev_visited_dirs = visited_dirs
            bazel_targets = [d.replace(workspace_dir, "/") for d in newly_visited_dirs]
        else:
            break

    merge_generated_build_files(generated_files)
    updated_pkgs.update(generated_files.keys())
    return updated_pkgs


def merge_generated_build_files(generated_files):
    buildfmt_path = runfiles.data_path("@dbx_build_tools//build_tools/buildfmt")

    merge_batch = []
    files_to_remove = set()

    for dirpath, intermediate_build_files in generated_files.items():
        # if `intermediate_build_files` contains only 'BUILD', it means
        # exactly one build generator generates the BUILD file directly
        # in the directory and there's no need to merge it
        output_file = os.path.join(dirpath, "BUILD")
        alt_output_file = os.path.join(dirpath, "BUILD.bazel")

        if intermediate_build_files == [output_file]:
            # always buildfmt even if not merging generated files
            run_cmd([buildfmt_path, output_file])
            continue

        with open(output_file, "w") as fd:
            fd.write(HEADER)

        # Extra crap to deal with OSX's shitty case insensitive file system.
        build_names = []
        for name in os.listdir(dirpath):
            if name.lower() == "build":
                build_names.append(name)

        if len(build_names) > 1:
            print(
                (
                    "WARNING: %s renamed to BUILD.bazel due to case "
                    "insensitivity name conflict" % output_file
                )
            )
            os.remove(output_file)
            output_file = alt_output_file
            with open(output_file, "w") as fd:
                fd.write(HEADER)
        elif os.path.isfile(alt_output_file):
            print("WARNING: %s removed" % alt_output_file)
            os.remove(alt_output_file)

        assert intermediate_build_files, dirpath
        assert output_file not in intermediate_build_files

        intermediate_build_files = sorted(set(intermediate_build_files))

        for filename in intermediate_build_files:
            merge_batch.append((output_file, filename, output_file))
            files_to_remove.add(filename)

        annotation_file = os.path.join(dirpath, "BUILD.in")

        if os.path.isfile(annotation_file):
            merge_batch.append((output_file, annotation_file, output_file))
        else:
            annotation_file = os.path.join(dirpath, "BUILD.in-gen-proto~")

            if os.path.isfile(annotation_file):
                merge_batch.append((output_file, annotation_file, output_file))

    # NOTE(jhance) Build merge merges in order, and this relies on that, since some of these
    # files in the batch have the same output file.
    build_merge.batch_merge_build_files(merge_batch)
    for f in files_to_remove:
        os.remove(f)
