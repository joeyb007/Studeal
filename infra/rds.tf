# Managed Postgres. pgvector is a supported RDS extension — migration 0001's
# CREATE EXTENSION works as-is, no parameter group needed.

resource "aws_db_subnet_group" "main" {
  name       = "studeal"
  subnet_ids = aws_subnet.public[*].id
}

# Generated inside Terraform, stored only in state (S3, encrypted) and SSM —
# never in a file or shell history. URL-safe special chars only, because the
# password gets embedded in DATABASE_URL.
resource "random_password" "db" {
  length           = 32
  special          = true
  override_special = "-_."
}

resource "aws_db_instance" "main" {
  identifier     = "studeal"
  engine         = "postgres"
  engine_version = "16"
  instance_class = "db.t4g.micro"

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "dealbot"
  username = "studeal"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false   # in a public subnet, but no public IP and
                                   # the SG only admits the tasks group anyway

  backup_retention_period = 7      # daily automated backups, kept a week
  deletion_protection     = true   # terraform destroy must be a two-step
                                   # decision for the thing holding the truth
  skip_final_snapshot     = false
  final_snapshot_identifier = "studeal-final"

  apply_immediately = true
}

# The app consumes one string; compose it here once so no human ever
# assembles a connection URL by hand.
resource "aws_ssm_parameter" "database_url" {
  name  = "/studeal/prod/DATABASE_URL"
  type  = "SecureString"
  value = "postgresql+asyncpg://${aws_db_instance.main.username}:${random_password.db.result}@${aws_db_instance.main.endpoint}/${aws_db_instance.main.db_name}"
}

output "rds_endpoint" {
  value = aws_db_instance.main.endpoint
}
