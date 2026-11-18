from compute_space.db.versioned.base import SqlFileMigration


class Migration0019DropImageVerificationState(SqlFileMigration):
    version = 19
    sql_file = "v0019_drop_image_verification_state.sql"
