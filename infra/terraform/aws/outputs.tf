# Outputs feed the Helm values, which is the whole handoff between IaC and deployment.
#
# No output carries a credential. The PostgreSQL password lives in Secrets Manager and the
# ARN is emitted instead, so `terraform output` is safe to paste into a terminal or a ticket.

output "cluster_name" {
  description = "EKS cluster name, for `aws eks update-kubeconfig`."
  value       = aws_eks_cluster.main.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.main.endpoint
}

output "kafka_bootstrap_brokers_tls" {
  description = "Set as externalServices.kafka.bootstrapServers in the Helm values."
  value       = aws_msk_cluster.main.bootstrap_brokers_tls
}

output "postgres_host" {
  value = aws_db_instance.postgres.address
}

output "postgres_secret_arn" {
  description = "Secrets Manager ARN. The password itself is never an output."
  value       = aws_secretsmanager_secret.postgres.arn
}

output "redis_primary_endpoint" {
  value = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "cold_tier_bucket" {
  value = aws_s3_bucket.cold_tier.id
}

output "helm_values_hint" {
  description = "The mapping from these outputs to Helm values, so the handoff is not guesswork."
  value       = <<-HINT
    helm upgrade --install fleet infra/helm/fleet-platform \
      -f infra/helm/fleet-platform/values-aws.yaml \
      --set externalServices.kafka.bootstrapServers=${aws_msk_cluster.main.bootstrap_brokers_tls} \
      --set externalServices.postgres.host=${aws_db_instance.postgres.address} \
      --set externalServices.redis.url=rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0
  HINT
}
