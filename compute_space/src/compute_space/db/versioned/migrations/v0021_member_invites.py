from compute_space.db.versioned.base import SqlFileMigration


class Migration0021MemberInvites(SqlFileMigration):
    version = 21
    sql_file = "v0021_member_invites.sql"
