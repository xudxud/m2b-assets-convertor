#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Maya 2020 worker for exporting static Blender-ready asset packages."""

from __future__ import print_function

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback


SCHEMA_VERSION = "1.0"
SUPPORTED_MATERIALS = {
    "VRayMtl": {
        "diffuse": ("color", "diffuseColor"),
        "opacity": ("opacityMap",),
        "opacity_invert": False,
    },
    "VRayCarPaintMtl": {
        "diffuse": ("base_color", "color"),
        "opacity": (),
        "opacity_invert": False,
    },
    "VRayFastSSS2": {
        "diffuse": ("diffuseTex", "color"),
        "diffuse_value": ("color", "diffuseTex"),
        "opacity": (),
        "opacity_invert": False,
    },
    "lambert": {
        "diffuse": ("color",),
        "opacity": ("transparency",),
        "opacity_invert": True,
    },
    "phong": {
        "diffuse": ("color",),
        "opacity": ("transparency",),
        "opacity_invert": True,
    },
    "blinn": {
        "diffuse": ("color",),
        "opacity": ("transparency",),
        "opacity_invert": True,
    },
    "standardSurface": {
        "diffuse": ("baseColor",),
        "opacity": ("opacity",),
        "opacity_invert": False,
    },
    "aiStandardSurface": {
        "diffuse": ("baseColor",),
        "opacity": ("opacity",),
        "opacity_invert": False,
    },
}


def emit(event, **values):
    values["event"] = event
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True)
    if isinstance(payload, unicode):
        payload = payload.encode("utf-8")
    sys.stdout.write(payload + "\n")
    sys.stdout.flush()


def as_unicode(value):
    if isinstance(value, unicode):
        return value
    try:
        return value.decode("mbcs")
    except (AttributeError, UnicodeDecodeError):
        return value.decode("utf-8", "replace")


def parse_bool(value):
    return value.lower() in ("1", "true", "yes", "on")


def safe_name(value):
    value = value.rsplit("|", 1)[-1].replace(":", "_")
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "material"


def load_plugin(cmds, plugin_name, required=True):
    try:
        if not cmds.pluginInfo(plugin_name, query=True, loaded=True):
            cmds.loadPlugin(plugin_name, quiet=True)
        emit("log", level="info", message="Plugin loaded: {}".format(plugin_name))
        return True
    except Exception as error:
        emit(
            "log",
            level="error" if required else "warning",
            message="Plugin unavailable: {} ({})".format(plugin_name, error),
        )
        if required:
            raise
        return False


def first_attribute(cmds, node, candidates):
    for attribute in candidates:
        if cmds.attributeQuery(attribute, node=node, exists=True):
            return attribute
    return None


def diffuse_input(cmds, material, adapter):
    """Prefer the first connected diffuse input, then use the value fallback."""
    existing = []
    for attribute in adapter["diffuse"]:
        if cmds.attributeQuery(attribute, node=material, exists=True):
            existing.append(attribute)
            texture, error = trace_file_texture(
                cmds, "{}.{}".format(material, attribute)
            )
            if error:
                return attribute, None, error
            if texture:
                return attribute, texture, None

    value_candidates = adapter.get("diffuse_value", adapter["diffuse"])
    for attribute in value_candidates:
        if attribute in existing or cmds.attributeQuery(
            attribute, node=material, exists=True
        ):
            return attribute, None, None
    return None, None, None


def source_channel(plug):
    attribute = plug.rsplit(".", 1)[-1].lower()
    if attribute.endswith("outalpha"):
        return "alpha"
    if attribute.endswith("outcolorr"):
        return "r"
    if attribute.endswith("outcolorg"):
        return "g"
    if attribute.endswith("outcolorb"):
        return "b"
    return "color"


def trace_file_texture(cmds, destination_plug):
    """Return the unique nearest upstream file plug, or an ambiguity error."""
    sources = cmds.listConnections(
        destination_plug, source=True, destination=False, plugs=True
    ) or []
    queue = [(plug, 0) for plug in sources]
    visited = set()
    matches = []
    nearest_depth = None

    while queue:
        source_plug, depth = queue.pop(0)
        if source_plug in visited:
            continue
        visited.add(source_plug)
        if nearest_depth is not None and depth > nearest_depth:
            break

        source_node = source_plug.split(".", 1)[0]
        if not cmds.objExists(source_node):
            continue
        if cmds.nodeType(source_node) == "file":
            nearest_depth = depth
            matches.append((source_node, source_plug))
            continue

        upstream = cmds.listConnections(
            source_node, source=True, destination=False, plugs=True
        ) or []
        queue.extend((plug, depth + 1) for plug in upstream)

    unique = {}
    for node, plug in matches:
        unique[node] = plug
    if len(unique) > 1:
        return None, "multiple nearest file textures: {}".format(
            ", ".join(sorted(unique.keys()))
        )
    if not unique:
        return None, None

    node = sorted(unique.keys())[0]
    plug = unique[node]
    raw_path = cmds.getAttr("{}.fileTextureName".format(node)) or ""
    try:
        color_space = cmds.getAttr("{}.colorSpace".format(node)) or ""
    except Exception:
        color_space = ""
    return {
        "node": node,
        "sourcePlug": plug,
        "channel": source_channel(plug),
        "sourcePath": raw_path,
        "colorSpace": color_space,
        "tiled": "<UDIM>" in raw_path.upper(),
    }, None


def read_value(cmds, plug, default):
    try:
        value = cmds.getAttr(plug)
        if isinstance(value, (list, tuple)) and value:
            if isinstance(value[0], (list, tuple)):
                return [float(item) for item in value[0][:3]]
            return [float(item) for item in value[:3]]
        return float(value)
    except Exception:
        return default


def scalar_value(value, default=1.0):
    if isinstance(value, (list, tuple)) and value:
        return float(sum(value[:3])) / float(min(3, len(value)))
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def connected_surface_material(cmds, shading_group):
    materials = cmds.listConnections(
        "{}.surfaceShader".format(shading_group), source=True, destination=False
    ) or []
    return materials[0] if materials else None


def assigned_materials(cmds, mesh_shapes):
    result = {}
    for shape in mesh_shapes:
        shading_groups = cmds.listConnections(shape, type="shadingEngine") or []
        for shading_group in shading_groups:
            material = connected_surface_material(cmds, shading_group)
            if material:
                result.setdefault(material, set()).add(shading_group)
    return result


def diagnostic_material(cmds, transfer_name):
    material = cmds.shadingNode("lambert", asShader=True, name=transfer_name)
    cmds.setAttr("{}.color".format(material), 1.0, 0.0, 0.65, type="double3")
    return material


def connect_texture_to_color(cmds, texture, destination):
    plug = texture["sourcePlug"]
    channel = texture["channel"]
    if channel == "color":
        cmds.connectAttr(plug, destination, force=True)
        return
    for component in ("R", "G", "B"):
        cmds.connectAttr(plug, destination + component, force=True)


def connect_transfer_opacity(cmds, texture, destination):
    """Connect directly so Maya's FBX exporter preserves the opacity texture."""
    plug = texture["sourcePlug"]
    if texture["channel"] == "color":
        cmds.connectAttr(plug, destination, force=True)
    else:
        for component in ("R", "G", "B"):
            cmds.connectAttr(plug, destination + component, force=True)


def build_transfer_materials(cmds, material_assignments):
    entries = []
    transfer_index = 0
    for index, source_material in enumerate(sorted(material_assignments.keys()), 1):
        source_type = cmds.nodeType(source_material)
        adapter = SUPPORTED_MATERIALS.get(source_type)
        transfer_index += 1
        transfer_name = "M2B_{:03d}_{}".format(
            transfer_index, safe_name(source_material)
        )
        entry = {
            "sourceName": source_material,
            "sourceType": source_type,
            "transferName": transfer_name,
            "status": "converted",
            "warnings": [],
            "diffuse": {"color": [0.5, 0.5, 0.5]},
            "opacity": {"value": 1.0, "invert": False},
            "shadingGroups": sorted(material_assignments[source_material]),
        }

        try:
            if not adapter:
                raise RuntimeError("unsupported material type: {}".format(source_type))

            diffuse_attr, diffuse_texture, diffuse_error = diffuse_input(
                cmds, source_material, adapter
            )
            if not diffuse_attr:
                raise RuntimeError("diffuse attribute not found")
            diffuse_plug = "{}.{}".format(source_material, diffuse_attr)
            if diffuse_error:
                raise RuntimeError("diffuse {}".format(diffuse_error))
            entry["diffuse"]["color"] = read_value(
                cmds, diffuse_plug, [0.5, 0.5, 0.5]
            )
            if diffuse_texture:
                if not diffuse_texture.get("colorSpace"):
                    diffuse_texture["colorSpace"] = "sRGB"
                entry["diffuse"]["texture"] = diffuse_texture

            opacity_attr = first_attribute(cmds, source_material, adapter["opacity"])
            if opacity_attr:
                opacity_plug = "{}.{}".format(source_material, opacity_attr)
                opacity_texture, opacity_error = trace_file_texture(cmds, opacity_plug)
                if opacity_error:
                    raise RuntimeError("opacity {}".format(opacity_error))
                opacity_value = scalar_value(read_value(cmds, opacity_plug, 1.0))
                entry["opacity"]["value"] = opacity_value
                entry["opacity"]["invert"] = adapter["opacity_invert"]
                if opacity_texture:
                    opacity_texture["colorSpace"] = "Non-Color"
                    entry["opacity"]["texture"] = opacity_texture

            transfer = cmds.shadingNode(
                "lambert", asShader=True, name=transfer_name
            )
            diffuse_texture = entry["diffuse"].get("texture")
            if diffuse_texture:
                connect_texture_to_color(
                    cmds, diffuse_texture, "{}.color".format(transfer)
                )
            else:
                color = entry["diffuse"]["color"]
                cmds.setAttr(
                    "{}.color".format(transfer),
                    color[0], color[1], color[2], type="double3"
                )

            opacity_texture = entry["opacity"].get("texture")
            if opacity_texture:
                connect_transfer_opacity(
                    cmds,
                    opacity_texture,
                    "{}.transparency".format(transfer),
                )
            else:
                opacity = entry["opacity"]["value"]
                if not entry["opacity"]["invert"]:
                    opacity = 1.0 - opacity
                cmds.setAttr(
                    "{}.transparency".format(transfer),
                    opacity, opacity, opacity, type="double3"
                )
        except Exception as error:
            entry["status"] = "failed"
            entry["diagnostic"] = True
            entry["warnings"].append(unicode(error))
            transfer = diagnostic_material(cmds, transfer_name)

        for shading_group in material_assignments[source_material]:
            cmds.connectAttr(
                "{}.outColor".format(transfer),
                "{}.surfaceShader".format(shading_group),
                force=True,
            )
        entries.append(entry)
        emit(
            "material-progress",
            current=index,
            total=len(material_assignments),
            material=source_material,
            target=transfer,
            status=entry["status"],
        )
    return entries


def render_mesh_shapes(cmds):
    result = []
    for shape in cmds.ls(type="mesh", long=True) or []:
        try:
            if not cmds.getAttr("{}.intermediateObject".format(shape)):
                result.append(shape)
        except Exception:
            pass
    return result


def world_bounds(cmds, nodes):
    bounds = cmds.exactWorldBoundingBox(nodes, ignoreInvisible=False)
    return [float(value) for value in bounds]


def validate_bounds(source_bounds, export_bounds):
    extent = max(
        abs(source_bounds[3] - source_bounds[0]),
        abs(source_bounds[4] - source_bounds[1]),
        abs(source_bounds[5] - source_bounds[2]),
        1.0,
    )
    tolerance = max(0.0001, extent * 0.0001)
    difference = max(
        abs(source - exported)
        for source, exported in zip(source_bounds, export_bounds)
    )
    if difference > tolerance:
        raise RuntimeError(
            "static snapshot bounds changed by {} (tolerance {})".format(
                difference, tolerance
            )
        )


def top_dag_roots(cmds, transforms):
    roots = set()
    for transform in transforms:
        current = transform
        parents = cmds.listRelatives(current, parent=True, fullPath=True) or []
        while parents:
            current = parents[0]
            parents = cmds.listRelatives(current, parent=True, fullPath=True) or []
        roots.add(current)
    return sorted(roots)


def reparent_children_and_delete(cmds, node):
    if not cmds.objExists(node):
        return
    parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
    parent = parents[0] if parents else None
    children = cmds.listRelatives(node, children=True, type="transform", fullPath=True) or []
    for child in children:
        try:
            if parent:
                cmds.parent(child, parent, absolute=True)
            else:
                cmds.parent(child, world=True, absolute=True)
        except Exception:
            pass
    cmds.delete(node)


def build_static_snapshot(cmds, mesh_shapes):
    transforms = set(
        (cmds.listRelatives(shape, parent=True, fullPath=True) or [None])[0]
        for shape in mesh_shapes
    )
    transforms = sorted(item for item in transforms if item)
    source_roots = top_dag_roots(cmds, transforms)
    export_group = cmds.group(empty=True, name="M2B_EXPORT")
    duplicate_roots = []
    for root in source_roots:
        duplicate = cmds.duplicate(
            root, returnRootsOnly=True, name="M2B_{}".format(safe_name(root))
        )[0]
        cmds.parent(duplicate, export_group, absolute=True)
        duplicate_roots.append(duplicate)

    cmds.delete(duplicate_roots, constructionHistory=True)

    non_mesh_shapes = []
    control_transforms = set()
    for shape in cmds.ls(export_group, dag=True, shapes=True, long=True) or []:
        if cmds.nodeType(shape) != "mesh":
            non_mesh_shapes.append(shape)
            parents = cmds.listRelatives(shape, parent=True, fullPath=True) or []
            if parents:
                control_transforms.add(parents[0])
        elif cmds.getAttr("{}.intermediateObject".format(shape)):
            non_mesh_shapes.append(shape)
    if non_mesh_shapes:
        cmds.delete(non_mesh_shapes)

    joints = cmds.ls(export_group, dag=True, type="joint", long=True) or []
    for joint in sorted(joints, key=lambda value: value.count("|"), reverse=True):
        reparent_children_and_delete(cmds, joint)

    for transform in sorted(
        control_transforms, key=lambda value: value.count("|"), reverse=True
    ):
        if not cmds.objExists(transform):
            continue
        mesh_children = cmds.listRelatives(
            transform, shapes=True, type="mesh", fullPath=True
        ) or []
        if not mesh_children:
            reparent_children_and_delete(cmds, transform)

    all_transforms = cmds.ls(export_group, dag=True, type="transform", long=True) or []
    for transform in sorted(
        all_transforms, key=lambda value: value.count("|"), reverse=True
    ):
        if not cmds.objExists(transform):
            continue
        descendant_meshes = cmds.listRelatives(
            transform, allDescendents=True, type="mesh", fullPath=True
        ) or []
        direct_meshes = cmds.listRelatives(
            transform, shapes=True, type="mesh", fullPath=True
        ) or []
        if not descendant_meshes and not direct_meshes:
            cmds.delete(transform)

    export_meshes = [
        shape for shape in (cmds.ls(export_group, dag=True, type="mesh", long=True) or [])
        if not cmds.getAttr("{}.intermediateObject".format(shape))
    ]
    invalid_history = []
    for shape in export_meshes:
        history = cmds.listHistory(shape, pruneDagObjects=True) or []
        deformers = []
        for node in history:
            try:
                if cmds.nodeType(node) in ("skinCluster", "blendShape", "cluster", "lattice"):
                    deformers.append(node)
            except Exception:
                pass
        if deformers:
            invalid_history.extend(deformers)

    if not export_meshes:
        raise RuntimeError("static snapshot contains no meshes")
    if invalid_history:
        raise RuntimeError(
            "static snapshot still contains deformers: {}".format(
                ", ".join(sorted(set(invalid_history)))
            )
        )
    return export_group, export_meshes, len(source_roots)


def resolve_source_texture(cmds, raw_path):
    raw_path = os.path.expandvars(as_unicode(raw_path))
    if os.path.isabs(raw_path):
        return os.path.normpath(raw_path)
    try:
        return os.path.normpath(cmds.workspace(expandName=raw_path))
    except Exception:
        return os.path.abspath(raw_path)


def udim_files(path):
    if "<UDIM>" not in path.upper():
        return [path] if os.path.isfile(path) else []
    pattern = re.sub(re.escape("<UDIM>"), "[0-9][0-9][0-9][0-9]", path,
                     flags=re.IGNORECASE)
    return sorted(glob.glob(pattern))


def invert_texture_files(source_pattern, files, destination_pattern, channel):
    from PySide2.QtGui import QImage

    for source_file in files:
        destination_file = destination_pattern
        if "<UDIM>" in source_pattern.upper():
            tile_match = re.search(r"(?<!\d)(1\d{3})(?!\d)", source_file)
            if not tile_match:
                raise RuntimeError("could not determine UDIM tile: {}".format(source_file))
            destination_file = re.sub(
                re.escape("<UDIM>"), tile_match.group(1), destination_pattern,
                flags=re.IGNORECASE
            )

        image = QImage(source_file)
        if image.isNull():
            raise RuntimeError("could not read opacity texture: {}".format(source_file))
        mode = QImage.InvertRgba if channel == "alpha" else QImage.InvertRgb
        image.invertPixels(mode)
        if not image.save(destination_file, "PNG"):
            raise RuntimeError(
                "could not save inverted opacity texture: {}".format(destination_file)
            )


def inverted_opacity_texture(cmds, texture, entry, package_dir, texture_dir, cache):
    source_pattern = resolve_source_texture(cmds, texture.get("sourcePath", ""))
    files = udim_files(source_pattern)
    if not files:
        raise RuntimeError("missing texture: {}".format(source_pattern))

    channel = texture.get("channel", "color")
    cache_key = (os.path.normcase(source_pattern), channel, texture["node"])
    if cache_key in cache:
        relative_pattern, destination_pattern, cloned_node = cache[cache_key]
    else:
        basename = os.path.basename(source_pattern)
        stem = os.path.splitext(basename)[0]
        digest = hashlib.md5(source_pattern.encode("utf-8")).hexdigest()[:8]
        destination_basename = "{}_{}_opacity_inverted.png".format(stem, digest)
        destination_pattern = os.path.join(texture_dir, destination_basename)
        invert_texture_files(source_pattern, files, destination_pattern, channel)

        clones = cmds.duplicate(
            texture["node"], inputConnections=True,
            name="M2B_opacity_file"
        ) or []
        if not clones:
            raise RuntimeError("could not duplicate opacity file node")
        cloned_node = clones[0]
        cmds.setAttr(
            "{}.fileTextureName".format(cloned_node), destination_pattern, type="string"
        )
        relative_pattern = os.path.relpath(
            destination_pattern, package_dir
        ).replace("\\", "/")
        cache[cache_key] = (relative_pattern, destination_pattern, cloned_node)

    source_attribute = texture["sourcePlug"].split(".", 1)[-1]
    cloned_texture = dict(texture)
    cloned_texture["node"] = cloned_node
    cloned_texture["sourcePlug"] = "{}.{}".format(cloned_node, source_attribute)
    cloned_texture["relativePath"] = relative_pattern
    cloned_texture["inverted"] = True
    connect_transfer_opacity(
        cmds, cloned_texture, "{}.transparency".format(entry["transferName"])
    )
    entry["opacity"]["texture"] = cloned_texture
    entry["opacity"]["invert"] = False


def copy_material_textures(cmds, entries, package_dir, enabled, invert_opacity):
    texture_dir = os.path.join(package_dir, "textures")
    copied = {}
    inverted = {}
    warnings = []
    if (enabled or invert_opacity) and not os.path.isdir(texture_dir):
        os.makedirs(texture_dir)

    for entry in entries:
        for section_name in ("diffuse", "opacity"):
            texture = entry.get(section_name, {}).get("texture")
            if not texture:
                continue
            if section_name == "opacity" and invert_opacity:
                try:
                    inverted_opacity_texture(
                        cmds, texture, entry, package_dir, texture_dir, inverted
                    )
                except Exception as error:
                    message = unicode(error)
                    entry["warnings"].append(message)
                    entry["status"] = "failed"
                    warnings.append(message)
                continue
            source_pattern = resolve_source_texture(cmds, texture.get("sourcePath", ""))
            files = udim_files(source_pattern)
            if not files:
                message = "missing texture: {}".format(source_pattern)
                entry["warnings"].append(message)
                entry["status"] = "failed"
                warnings.append(message)
                texture["missing"] = True
                continue

            if not enabled:
                texture["relativePath"] = source_pattern.replace("\\", "/")
                continue

            cache_key = os.path.normcase(source_pattern)
            if cache_key in copied:
                relative_pattern, destination_pattern = copied[cache_key]
            else:
                basename = os.path.basename(source_pattern)
                destination_pattern = os.path.join(texture_dir, basename)
                if os.path.exists(destination_pattern) and os.path.normcase(
                    destination_pattern
                ) != os.path.normcase(source_pattern):
                    digest = hashlib.md5(source_pattern.encode("utf-8")).hexdigest()[:8]
                    stem, extension = os.path.splitext(basename)
                    basename = "{}_{}{}".format(stem, digest, extension)
                    destination_pattern = os.path.join(texture_dir, basename)

                try:
                    for source_file in files:
                        if "<UDIM>" in source_pattern.upper():
                            tile_match = re.search(r"(?<!\d)(1\d{3})(?!\d)", source_file)
                            if not tile_match:
                                continue
                            destination_file = re.sub(
                                re.escape("<UDIM>"), tile_match.group(1),
                                destination_pattern, flags=re.IGNORECASE
                            )
                        else:
                            destination_file = destination_pattern
                        shutil.copy2(source_file, destination_file)
                except (IOError, OSError) as error:
                    message = "could not copy texture {}: {}".format(
                        source_pattern, error
                    )
                    entry["warnings"].append(message)
                    entry["status"] = "failed"
                    warnings.append(message)
                    continue

                relative_pattern = os.path.relpath(
                    destination_pattern, package_dir
                ).replace("\\", "/")
                copied[cache_key] = (relative_pattern, destination_pattern)

            texture["relativePath"] = relative_pattern
            try:
                cmds.setAttr(
                    "{}.fileTextureName".format(texture["node"]),
                    destination_pattern,
                    type="string",
                )
            except Exception as error:
                warning = "could not redirect {}: {}".format(texture["node"], error)
                entry["warnings"].append(warning)
                warnings.append(warning)
    return warnings


def ensure_diagnostic_materials(cmds, entries):
    for entry in entries:
        if entry["status"] != "failed" or entry.get("diagnostic"):
            continue
        diagnostic_name = "{}_diagnostic".format(entry["transferName"])
        diagnostic = diagnostic_material(cmds, diagnostic_name)
        for shading_group in entry["shadingGroups"]:
            cmds.connectAttr(
                "{}.outColor".format(diagnostic),
                "{}.surfaceShader".format(shading_group),
                force=True,
            )
        entry["transferName"] = diagnostic
        entry["diagnostic"] = True


def output_package(input_path, output_dir, overwrite):
    directory = output_dir or os.path.dirname(input_path)
    base_name = os.path.splitext(os.path.basename(input_path))[0] + "_converted"
    package_dir = os.path.join(directory, base_name)
    if os.path.isdir(package_dir):
        if overwrite == "skip":
            existing_fbx = os.path.join(package_dir, base_name + ".fbx")
            existing_manifest = os.path.join(package_dir, base_name + ".m2b.json")
            if os.path.isfile(existing_fbx) and os.path.isfile(existing_manifest):
                return package_dir, True
        if overwrite == "rename":
            index = 1
            while os.path.exists("{}_{}".format(package_dir, index)):
                index += 1
            package_dir = "{}_{}".format(package_dir, index)
    if not os.path.isdir(package_dir):
        os.makedirs(package_dir)
    return package_dir, False


def atomic_json(path, value):
    temporary = path + ".tmp"
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    if isinstance(payload, unicode):
        payload = payload.encode("utf-8")
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.write("\n")
    if os.path.exists(path):
        os.remove(path)
    os.rename(temporary, path)


def export_fbx(cmds, mel, export_group, output_path, embedded_textures):
    temporary = os.path.join(os.path.dirname(output_path), ".m2b-export.tmp.fbx")
    if os.path.exists(temporary):
        os.remove(temporary)
    output_mel_path = temporary.replace("\\", "/").replace('"', '\\"')
    options = (
        "FBXResetExport",
        "FBXExportSkins -v false",
        "FBXExportShapes -v false",
        "FBXExportConstraints -v false",
        "FBXExportCameras -v false",
        "FBXExportLights -v false",
        "FBXExportBakeComplexAnimation -v false",
        "FBXExportSmoothingGroups -v true",
        "FBXExportSmoothMesh -v true",
        "FBXExportInputConnections -v true",
        "FBXExportEmbeddedTextures -v {}".format(
            "true" if embedded_textures else "false"
        ),
    )
    for option in options:
        mel.eval(option)

    cmds.select(export_group, replace=True, hierarchy=True)
    mel.eval('FBXExport -f "{}" -s'.format(output_mel_path))
    if not os.path.isfile(temporary) or os.path.getsize(temporary) <= 0:
        raise RuntimeError("FBX exporter did not create a valid temporary file")
    if os.path.exists(output_path):
        os.remove(output_path)
    os.rename(temporary, output_path)


def clean_manifest_textures(entries):
    for entry in entries:
        entry.pop("diagnostic", None)
        for section_name in ("diffuse", "opacity"):
            texture = entry.get(section_name, {}).get("texture")
            if texture:
                texture.pop("node", None)
                texture.pop("sourcePlug", None)


def run(args):
    started = time.time()
    input_path = os.path.abspath(as_unicode(args.input))
    output_dir = os.path.abspath(as_unicode(args.output_dir)) if args.output_dir else None
    if not os.path.isfile(input_path):
        raise IOError("Input file not found: {}".format(input_path))

    package_dir, skipped = output_package(input_path, output_dir, args.overwrite)
    package_name = os.path.basename(package_dir)
    fbx_path = os.path.join(package_dir, package_name + ".fbx")
    manifest_path = os.path.join(package_dir, package_name + ".m2b.json")
    if skipped:
        emit("completed", input=input_path, output=fbx_path, manifest=manifest_path,
             skipped=True, success=True, elapsed=round(time.time() - started, 2))
        return 0

    import maya.standalone
    maya.standalone.initialize(name="python")
    try:
        import maya.cmds as cmds
        import maya.mel as mel

        emit("initialized", mayaVersion=cmds.about(version=True), input=input_path)
        load_plugin(cmds, "vrayformaya", required=False)
        load_plugin(cmds, "mtoa", required=False)
        load_plugin(cmds, "fbxmaya", required=True)
        cmds.file(input_path, open=True, force=True, prompt=False)
        emit("scene-opened", input=input_path)
        unknown_plugins = sorted(cmds.unknownPlugin(query=True, list=True) or [])
        if unknown_plugins:
            emit(
                "scene-warning",
                kind="missing-legacy-plugins",
                message="Scene references {} unavailable legacy plugins".format(
                    len(unknown_plugins)
                ),
                plugins=unknown_plugins,
            )

        meshes = render_mesh_shapes(cmds)
        if not meshes:
            raise RuntimeError("scene contains no renderable meshes")
        source_bounds = world_bounds(cmds, meshes)
        assignments = assigned_materials(cmds, meshes)
        emit("material-scan", total=len(assignments))
        materials = build_transfer_materials(cmds, assignments)
        copy_material_textures(
            cmds, materials, package_dir, args.copy_textures, args.invert_opacity
        )
        ensure_diagnostic_materials(cmds, materials)

        export_group, export_meshes, root_count = build_static_snapshot(cmds, meshes)
        export_bounds = world_bounds(cmds, export_meshes)
        validate_bounds(source_bounds, export_bounds)
        mesh_count = len(export_meshes)
        emit("export-started", output=fbx_path, meshes=mesh_count)
        export_fbx(cmds, mel, export_group, fbx_path, args.embedded_textures)

        failed_materials = [
            entry for entry in materials if entry["status"] == "failed"
        ]
        warning_count = sum(len(entry["warnings"]) for entry in materials)
        if unknown_plugins:
            warning_count += 1
        clean_manifest_textures(materials)
        manifest = {
            "schemaVersion": SCHEMA_VERSION,
            "generator": {"name": "M2B", "version": "0.2.3"},
            "asset": {
                "source": input_path,
                "fbx": os.path.basename(fbx_path),
                "geometryMode": "staticSnapshot",
                "meshCount": mesh_count,
                "sourceRootCount": root_count,
                "sourceBounds": source_bounds,
                "exportBounds": export_bounds,
                "unknownPlugins": unknown_plugins,
                "opacityTexturesInverted": args.invert_opacity,
            },
            "materials": materials,
            "summary": {
                "success": not failed_materials,
                "materialCount": len(materials),
                "materialFailed": len(failed_materials),
                "warningCount": warning_count,
                "elapsed": round(time.time() - started, 2),
            },
        }
        atomic_json(manifest_path, manifest)
        atomic_json(os.path.join(package_dir, "conversion_report.json"), manifest)
        emit(
            "completed",
            input=input_path,
            output=fbx_path,
            manifest=manifest_path,
            skipped=False,
            success=not failed_materials,
            converted=len(materials) - len(failed_materials),
            materialFailed=len(failed_materials),
            materialTotal=len(materials),
            warnings=warning_count,
            meshes=mesh_count,
            elapsed=round(time.time() - started, 2),
        )
        return 0 if not failed_materials else 2
    finally:
        maya.standalone.uninitialize()


def main():
    parser = argparse.ArgumentParser(description="M2B Maya Worker")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--copy-textures", type=parse_bool, default=True)
    parser.add_argument("--invert-opacity", type=parse_bool, default=False)
    parser.add_argument("--embedded-textures", type=parse_bool, default=False)
    parser.add_argument(
        "--overwrite", choices=("skip", "overwrite", "rename"), default="rename"
    )
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as error:
        emit("failed", message=unicode(error), traceback=traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
