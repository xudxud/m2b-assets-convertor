"""Import an M2B manifest and rebuild its FBX transfer materials."""

import glob
import json
import os
import re

import bpy
from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper


bl_info = {
    "name": "M2B Asset Importer",
    "author": "M2B",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "File > Import > M2B Asset",
    "description": "Import an M2B FBX and rebuild transfer materials",
    "category": "Import-Export",
}


_FAILED_STATUSES = {"error", "failed", "failure", "invalid", "missing", "skipped"}
_SUFFIX_RE = re.compile(r"\.\d{3,}$")


def _warn(messages, text):
    messages.append(text)
    print("M2B: WARNING: " + text)


def _resolve_path(base_dir, value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = os.path.expandvars(os.path.expanduser(value.strip()))
    if value.startswith("//"):
        value = value[2:]
    if not os.path.isabs(value):
        value = os.path.join(base_dir, value)
    return os.path.normpath(value)


def _fbx_path(manifest_path, manifest):
    asset = manifest.get("asset")
    value = asset.get("fbx") if isinstance(asset, dict) else None
    if isinstance(value, dict):
        value = value.get("relativePath") or value.get("path")
    path = _resolve_path(os.path.dirname(manifest_path), value)
    if path:
        return path
    suffix = ".m2b.json"
    if manifest_path.lower().endswith(suffix):
        return manifest_path[:-len(suffix)] + ".fbx"
    return os.path.splitext(manifest_path)[0] + ".fbx"


def _number(value, default):
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _color(value):
    if isinstance(value, dict):
        values = [value.get(key) for key in ("r", "g", "b", "a")]
    elif isinstance(value, (list, tuple)):
        values = list(value[:4])
    else:
        values = []
    defaults = (0.8, 0.8, 0.8, 1.0)
    return tuple(_number(values[i], defaults[i]) if i < len(values) else defaults[i]
                 for i in range(4))


def _texture_spec(section):
    if not isinstance(section, dict):
        return None
    texture = section.get("texture")
    return texture if isinstance(texture, dict) else None


def _set_color_space(image, requested, messages):
    if not isinstance(requested, str) or not requested.strip():
        return
    requested = requested.strip()
    candidates = [requested]
    aliases = {
        "linear": "Linear Rec.709",
        "non-color": "Non-Color",
        "noncolor": "Non-Color",
        "raw": "Non-Color",
        "srgb": "sRGB",
    }
    alias = aliases.get(requested.lower())
    if alias and alias not in candidates:
        candidates.append(alias)
    for candidate in candidates:
        try:
            image.colorspace_settings.name = candidate
            return
        except TypeError:
            pass
    _warn(messages, "Unsupported color space '%s' for %s" % (requested, image.name))


def _udim_load_path(path):
    if "<UDIM>" not in path.upper():
        return path, None
    token_re = re.compile(re.escape("<UDIM>"), re.IGNORECASE)
    pattern = token_re.sub("[0-9][0-9][0-9][0-9]", path)
    matches = sorted(glob.glob(pattern))
    return (matches[0] if matches else None), path


def _load_image(texture, base_dir, image_cache, messages):
    relative_path = texture.get("relativePath")
    path = _resolve_path(base_dir, relative_path)
    if not path:
        _warn(messages, "Texture has no relativePath")
        return None

    tiled = bool(texture.get("tiled")) or "<UDIM>" in path.upper()
    load_path, token_path = _udim_load_path(path)
    if not load_path or not os.path.isfile(load_path):
        _warn(messages, "Missing texture: " + path)
        return None

    color_space = texture.get("colorSpace")
    cache_key = (os.path.normcase(path), str(color_space), tiled)
    if cache_key in image_cache:
        return image_cache[cache_key]

    try:
        image = bpy.data.images.load(load_path, check_existing=False)
    except (RuntimeError, OSError) as exc:
        _warn(messages, "Could not load texture %s: %s" % (path, exc))
        return None

    if tiled:
        try:
            image.source = 'TILED'
            if token_path:
                image.filepath = token_path
        except (TypeError, ValueError) as exc:
            _warn(messages, "Could not enable UDIM for %s: %s" % (path, exc))
    _set_color_space(image, color_space, messages)
    image_cache[cache_key] = image
    return image


def _new_node(nodes, node_type, fallback=None):
    try:
        return nodes.new(node_type)
    except RuntimeError:
        if fallback:
            return nodes.new(fallback)
        raise


def _channel_socket(nodes, links, texture_node, channel, messages):
    channel = channel.lower() if isinstance(channel, str) else "color"
    if channel == "color":
        return texture_node.outputs.get("Color")
    if channel == "alpha":
        return texture_node.outputs.get("Alpha")
    if channel in {"r", "g", "b"}:
        separate = _new_node(nodes, "ShaderNodeSeparateColor", "ShaderNodeSeparateRGB")
        separate.label = "M2B Channel " + channel.upper()
        links.new(texture_node.outputs["Color"], separate.inputs[0])
        names = {"r": ("Red", "R"), "g": ("Green", "G"), "b": ("Blue", "B")}
        for name in names[channel]:
            socket = separate.outputs.get(name)
            if socket:
                return socket
    _warn(messages, "Unsupported texture channel '%s'; using color" % channel)
    return texture_node.outputs.get("Color")


def _texture_socket(nodes, links, texture, base_dir, image_cache, messages,
                    default_channel):
    image = _load_image(texture, base_dir, image_cache, messages)
    if not image:
        return None
    node = nodes.new("ShaderNodeTexImage")
    node.image = image
    node.label = os.path.basename(str(texture.get("relativePath", "M2B Texture")))
    return _channel_socket(
        nodes, links, node, texture.get("channel", default_channel), messages)


def _set_transparency_mode(material, messages):
    if hasattr(material, "surface_render_method"):
        for value in ("DITHERED", "BLENDED"):
            try:
                material.surface_render_method = value
                return
            except (TypeError, ValueError):
                pass
        _warn(messages, "Could not set surface render method on " + material.name)
    elif hasattr(material, "blend_method"):
        try:
            material.blend_method = 'BLEND'
        except (TypeError, ValueError):
            _warn(messages, "Could not set blend method on " + material.name)


def _rebuild_material(material, entry, base_dir, image_cache, messages):
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (500, 0)
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (160, 0)
    links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    diffuse = entry.get("diffuse")
    if not isinstance(diffuse, dict):
        diffuse = {}
    base_color = principled.inputs.get("Base Color")
    if base_color:
        base_color.default_value = _color(diffuse.get("color"))
        texture = _texture_spec(diffuse)
        if texture:
            socket = _texture_socket(
                nodes, links, texture, base_dir, image_cache, messages, "color")
            if socket:
                links.new(socket, base_color)

    opacity = entry.get("opacity")
    if not isinstance(opacity, dict):
        opacity = {}
    alpha = principled.inputs.get("Alpha")
    opacity_value = _number(opacity.get("value"), 1.0)
    opacity_texture = _texture_spec(opacity)
    opacity_socket = None
    if opacity_texture:
        opacity_socket = _texture_socket(
            nodes, links, opacity_texture, base_dir, image_cache, messages, "alpha")

    if alpha:
        if opacity.get("invert"):
            invert = nodes.new("ShaderNodeMath")
            invert.operation = 'SUBTRACT'
            invert.label = "M2B Invert Opacity"
            invert.inputs[0].default_value = 1.0
            invert.inputs[1].default_value = opacity_value
            if opacity_socket:
                links.new(opacity_socket, invert.inputs[1])
            links.new(invert.outputs[0], alpha)
        elif opacity_socket:
            links.new(opacity_socket, alpha)
        else:
            alpha.default_value = opacity_value

    effective_value = 1.0 - opacity_value if opacity.get("invert") else opacity_value
    if opacity_texture or effective_value < 0.999:
        _set_transparency_mode(material, messages)


def _find_material(transfer_name, imported, claimed):
    exact = [mat for mat in imported if mat.name == transfer_name and mat not in claimed]
    if exact:
        return exact[0]
    candidates = []
    prefix = transfer_name + "."
    for material in imported:
        if material in claimed or not material.name.startswith(prefix):
            continue
        suffix = material.name[len(transfer_name):]
        if _SUFFIX_RE.fullmatch(suffix):
            candidates.append(material)
    return sorted(candidates, key=lambda mat: mat.name)[0] if candidates else None


def _status_allows(entry):
    status = entry.get("status")
    return not (isinstance(status, str) and status.strip().lower() in _FAILED_STATUSES)


class IMPORT_SCENE_OT_m2b_asset(bpy.types.Operator, ImportHelper):
    """Import an FBX described by an M2B JSON manifest"""

    bl_idname = "import_scene.m2b_asset"
    bl_label = "Import M2B Asset"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".m2b.json"
    filter_glob: StringProperty(default="*.m2b.json", options={'HIDDEN'})

    def execute(self, context):
        manifest_path = os.path.abspath(self.filepath)
        try:
            with open(manifest_path, "r", encoding="utf-8-sig") as handle:
                manifest = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.report({'ERROR'}, "M2B manifest could not be read: %s" % exc)
            return {'CANCELLED'}

        if not isinstance(manifest, dict):
            self.report({'ERROR'}, "M2B manifest root must be an object")
            return {'CANCELLED'}
        materials = manifest.get("materials")
        if not isinstance(materials, list):
            self.report({'ERROR'}, "M2B manifest materials must be an array")
            return {'CANCELLED'}

        fbx_path = _fbx_path(manifest_path, manifest)
        if not os.path.isfile(fbx_path):
            self.report({'ERROR'}, "M2B FBX not found: " + fbx_path)
            return {'CANCELLED'}
        if not hasattr(bpy.ops.import_scene, "fbx"):
            self.report({'ERROR'}, "Blender FBX importer is unavailable")
            return {'CANCELLED'}

        material_ids_before = {material.as_pointer() for material in bpy.data.materials}
        try:
            result = bpy.ops.import_scene.fbx(filepath=fbx_path)
        except RuntimeError as exc:
            self.report({'ERROR'}, "M2B FBX import failed: %s" % exc)
            return {'CANCELLED'}
        if 'FINISHED' not in result:
            self.report({'ERROR'}, "M2B FBX import did not finish")
            return {'CANCELLED'}

        imported = [material for material in bpy.data.materials
                    if material.as_pointer() not in material_ids_before]
        base_dir = os.path.dirname(manifest_path)
        image_cache = {}
        messages = []
        claimed = set()
        rebuilt = 0
        unmatched = 0
        skipped = 0

        for entry in materials:
            if not isinstance(entry, dict):
                skipped += 1
                _warn(messages, "Ignored non-object material entry")
                continue
            transfer_name = entry.get("transferName")
            if not isinstance(transfer_name, str) or not transfer_name:
                skipped += 1
                _warn(messages, "Ignored material entry without transferName")
                continue
            if not _status_allows(entry):
                skipped += 1
                continue
            material = _find_material(transfer_name, imported, claimed)
            if not material:
                unmatched += 1
                _warn(messages, "No newly imported material matches " + transfer_name)
                continue
            try:
                _rebuild_material(material, entry, base_dir, image_cache, messages)
            except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                _warn(messages, "Could not rebuild %s: %s" % (material.name, exc))
                continue
            claimed.add(material)
            rebuilt += 1

        version = manifest.get("schemaVersion", "unspecified")
        summary = ("M2B schema %s: imported FBX, rebuilt %d material(s), "
                   "%d unmatched, %d skipped, %d warning(s)" %
                   (version, rebuilt, unmatched, skipped, len(messages)))
        print("M2B: " + summary)
        self.report({'WARNING'} if messages else {'INFO'}, summary)
        return {'FINISHED'}


def _menu_import(self, context):
    self.layout.operator(IMPORT_SCENE_OT_m2b_asset.bl_idname, text="M2B Asset (.m2b.json)")


classes = (IMPORT_SCENE_OT_m2b_asset,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
