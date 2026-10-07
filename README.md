# retpack

A Fabric modpack for vanilla survival with shaders, distant terrain, and building tools. Forked from [ExPack](https://github.com/MAttila42/expack).

Minecraft **26.2**, Fabric **0.19.5**, Java **25**.

## Contents

- **Performance:** Sodium, Lithium, C2ME, FerriteCore, ImmediatelyFast, BadOptimizations, Entity Culling, More Culling, and Debugify.
- **Graphics:** Iris, Voxy, LambDynamicLights, Complementary Reimagined (default), and Photon.
- **Building:** Litematica, MiniHUD, Tweakeroo, and MaLiLib.
- **Interface:** Mod Menu, Shulker Box Tooltip, Chat Heads, and Better Mount HUD.
- **Optional lab:** Carpet for farm testing and Axiom for creative building.
- **Optional terrain generation:** Voxy WorldGen fills unseen terrain. Disabled by default because of a [reported shutdown hang with C2ME](https://github.com/iSeeEthan/voxy_worldgen_v2/issues/99).

Mod versions and download hashes are tracked with [packwiz](https://packwiz.infra.link/) in `pack/`. Sodium, Iris, and Voxy are pinned together for shader compatibility.

## Install

On Windows, run from the repository folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

In Legacy Launcher, use the printed game directory, disable subfolders, select `fabric-loader-0.19.5-26.2`, and use Recommended Java.

Use `-Lab -Destination <folder>` for a separate lab instance, or `-WorldGen` to include terrain generation. Keep those switches when updating; omitted optional mods are removed. Existing settings and worlds are preserved.

With packwiz installed, run `scripts/build.ps1` to export survival and lab `.mrpack` files into `dist/`.
