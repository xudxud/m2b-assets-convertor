# M2B Blender Importer

Single-file Blender addon for Blender 4.2 through 5.2. It imports the FBX referenced by an `.m2b.json` manifest and rebuilds newly imported transfer materials with Principled BSDF nodes.

## Install

1. In Blender, open `Edit > Preferences > Add-ons` (or `Extensions > Install from Disk`, depending on the Blender version).
2. Install `m2b_importer.py` and enable **M2B Asset Importer**.
3. Use `File > Import > M2B Asset (.m2b.json)`.

The Blender FBX importer must be available. The addon invokes `bpy.ops.import_scene.fbx` and does not replace Blender's FBX support.

## Manifest

The importer expects this shape:

```json
{
  "schemaVersion": 1,
  "asset": {
    "fbx": "asset.fbx"
  },
  "materials": [
    {
      "transferName": "Body",
      "status": "ok",
      "diffuse": {
        "color": [1.0, 1.0, 1.0, 1.0],
        "texture": {
          "relativePath": "textures/body_<UDIM>.png",
          "channel": "color",
          "colorSpace": "sRGB",
          "tiled": true
        }
      },
      "opacity": {
        "value": 1.0,
        "invert": false,
        "texture": {
          "relativePath": "textures/body_opacity.png",
          "channel": "alpha",
          "colorSpace": "Non-Color"
        }
      }
    }
  ]
}
```

`asset.fbx` and texture `relativePath` values are resolved relative to the manifest directory. If `asset.fbx` is absent, `name.m2b.json` resolves to sibling `name.fbx`.

Texture channels `color`, `alpha`, `r`, `g`, and `b` are supported. Opacity inversion uses a Math/Subtract node. UDIM textures can use the `<UDIM>` token or set `tiled` to `true`. Missing textures and unmatched materials are reported in Blender's status area and with details in the system console.

Only materials created by the FBX import operation are eligible for rebuilding. Material names are matched exactly first; Blender suffixes such as `.001` are considered only when no exact newly imported name exists. Entries with explicit failure statuses (`error`, `failed`, `failure`, `invalid`, `missing`, or `skipped`) are ignored.

Transparency mode is selected by capability detection: `surface_render_method` on current Blender versions, otherwise legacy `blend_method`.
