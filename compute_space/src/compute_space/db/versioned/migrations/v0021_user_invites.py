from compute_space.db.versioned.base import SqlFileMigration


class Migration0021UserInvites(SqlFileMigration):
    version = 21
    sql_file = "v0021_user_invites.sql"
