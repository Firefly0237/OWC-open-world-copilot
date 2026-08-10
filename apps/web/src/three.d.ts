// three ships ESM without a bundled declaration under this version, and we only use a handful of
// classes (Sprite, Group, CanvasTexture, SpriteMaterial) loosely. Declare it as any to keep
// vue-tsc happy without pinning a possibly-mismatched @types/three.
declare module "three";
