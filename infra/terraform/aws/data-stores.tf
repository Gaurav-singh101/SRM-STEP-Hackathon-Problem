# Managed data stores. Encrypted at rest and in transit throughout, because CLAUDE.md S11
# requires AES-256 at rest and TLS in transit, and both are a configuration flag here.

data "aws_caller_identity" "current" {}

resource "random_password" "postgres" {
  length  = 32
  special = true
  # Excludes characters that break a DSN or a shell. A password that cannot be used is a
  # password someone replaces with a weaker one.
  override_special = "!#%&*()-_=+[]{}:?"
}

resource "aws_secretsmanager_secret" "postgres" {
  name = "${local.name}/postgres"
  # Zero rather than the default 30 days, only because an ephemeral environment is created
  # and destroyed repeatedly and a recovery window blocks reusing the same name.
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "postgres" {
  secret_id = aws_secretsmanager_secret.postgres.id
  secret_string = jsonencode({
    username = "fleet"
    password = random_password.postgres.result
    host     = aws_db_instance.postgres.address
    port     = 5432
    dbname   = "fleet"
  })
}

resource "aws_db_subnet_group" "postgres" {
  name       = "${local.name}-postgres"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "postgres" {
  name   = "${local.name}-postgres16"
  family = "postgres16"

  parameter {
    # Force TLS. Without it a client can silently negotiate plaintext inside the VPC, which
    # satisfies no compliance requirement while appearing to.
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    # Log anything slower than a second. The API p95 target is 200 ms, so a second is
    # unambiguously wrong and worth a log line.
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "aws_db_instance" "postgres" {
  identifier     = "${local.name}-postgres"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.postgres_instance_class

  allocated_storage     = var.postgres_allocated_storage_gb
  max_allocated_storage = var.postgres_allocated_storage_gb * 4
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "fleet"
  username = "fleet"
  password = random_password.postgres.result

  db_subnet_group_name   = aws_db_subnet_group.postgres.name
  vpc_security_group_ids = [aws_security_group.postgres.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name

  # Never reachable from the internet. The most consequential line in this file.
  publicly_accessible = false

  multi_az                = var.environment == "prod"
  backup_retention_period = var.environment == "prod" ? 14 : 1
  deletion_protection     = var.environment == "prod"

  # An ephemeral environment must be destroyable without a manual snapshot step, or
  # `make cloud-down` stops working and the bill does not stop.
  skip_final_snapshot = var.environment != "prod"

  performance_insights_enabled    = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
  auto_minor_version_upgrade      = true

  tags = { Name = "${local.name}-postgres" }
}

# --- Redis -----------------------------------------------------------------

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = "${local.name}-redis"
  description          = "Hot vehicle state, dedup window, rate limits"

  # 7.2 deliberately, and NOT higher. Redis changed licence at 7.4 (RSALv2/SSPL) and this
  # project pins the last BSD line - ADR-0002 and CLAUDE.md S4. Do not bump without an ADR;
  # Valkey is the drop-in if 7.2 support becomes a problem.
  engine         = "redis"
  engine_version = "7.2"
  node_type      = var.redis_node_type

  num_cache_clusters         = var.environment == "prod" ? 2 : 1
  automatic_failover_enabled = var.environment == "prod"

  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true

  # The cache is disposable and rebuildable by design (CLAUDE.md S8), so a snapshot would be
  # paying to preserve state we are content to lose.
  snapshot_retention_limit = 0

  tags = { Name = "${local.name}-redis" }
}

# --- Kafka (MSK) -----------------------------------------------------------
#
# D8 remains open: MSK here versus self-managed Strimzi on EKS. MSK is provisioned because
# it is fewer moving parts for a time-boxed benchmark; the portability argument for Strimzi
# is recorded in the open decisions log. The application cannot tell the difference, which is
# the point - it takes a bootstrap server from configuration.

resource "aws_msk_configuration" "main" {
  name           = "${local.name}-config"
  kafka_versions = ["3.9.x"]

  server_properties = <<-PROPERTIES
    auto.create.topics.enable=false
    default.replication.factor=3
    min.insync.replicas=2
    num.partitions=${var.kafka_partitions}
    unclean.leader.election.enable=false
    log.retention.hours=168
  PROPERTIES
}

resource "aws_msk_cluster" "main" {
  cluster_name           = local.name
  kafka_version          = "3.9.x"
  number_of_broker_nodes = var.kafka_broker_count

  broker_node_group_info {
    instance_type   = var.kafka_instance_type
    client_subnets  = aws_subnet.private[*].id
    security_groups = [aws_security_group.kafka.id]

    storage_info {
      ebs_storage_info {
        # At the measured 479 bytes/event, 100K events/s is 4.14 TB/day. Sized for a
        # time-boxed benchmark window, not for 7-day retention at full rate, which would
        # need roughly 29 TB and is not affordable here.
        volume_size = 100
      }
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.main.arn
    revision = aws_msk_configuration.main.latest_revision
  }

  encryption_info {
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  # mTLS on the device-to-broker path, which CLAUDE.md S11 asks for, is left unset because it
  # needs an ACM Private CA at roughly $400/month. The gap is recorded in docs/STRIDE.md as
  # S3 - the highest-ranked open threat - rather than quietly omitted.
  # client_authentication { tls { certificate_authority_arns = [...] } }

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.kafka.name
      }
    }
  }

  tags = { Name = local.name }
}

resource "aws_cloudwatch_log_group" "kafka" {
  name              = "/aws/msk/${local.name}"
  retention_in_days = 7
}

# --- Object storage --------------------------------------------------------
#
# For the cold tier if D6 chooses Parquet. Reached through an S3-compatible interface in
# application code, never the AWS SDK directly, so MinIO substitutes locally.

resource "aws_s3_bucket" "cold_tier" {
  bucket = "${local.name}-cold-tier-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cold_tier" {
  bucket = aws_s3_bucket.cold_tier.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "cold_tier" {
  bucket = aws_s3_bucket.cold_tier.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "cold_tier" {
  bucket = aws_s3_bucket.cold_tier.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cold_tier" {
  bucket = aws_s3_bucket.cold_tier.id

  rule {
    id     = "tier-and-expire"
    status = "Enabled"

    filter {}

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }

    # Retention is a documented data-class decision enforced by a rule rather than by
    # someone remembering (CLAUDE.md S11).
    expiration {
      days = 730
    }
  }
}
