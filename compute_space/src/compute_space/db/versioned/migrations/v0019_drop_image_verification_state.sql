-- Signature requirements now follow the source repo, not the last deployed image's registry.
ALTER TABLE apps DROP COLUMN prebuilt_image_verified;
