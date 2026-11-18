-- v18: add ``apps.prebuilt_image_verified``: 1 when the last prebuilt image the
-- app was deployed on had its signature verified, 0 when it was pulled
-- unverified or the app has never run a prebuilt image. A build (the app
-- doesn't use a prebuilt image, or its image couldn't be fetched) leaves it as
-- it was. An update that would take an app from 1 to an unverified image needs
-- the owner's approval (see core/image_verification.py).
--
-- Every existing app starts at 0: no released openhost has verified an image
-- yet, so no app is running a verified one.

ALTER TABLE apps ADD COLUMN prebuilt_image_verified INTEGER NOT NULL DEFAULT 0;
