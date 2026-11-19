from compute_space.db.versioned.base import SqlFileMigration


class Migration0020DropImageVerificationState(SqlFileMigration):
    version = 20
    sql_file = "v0020_drop_image_verification_state.sql"
