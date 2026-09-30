variable "region" {
  description = "AWS region. ap-south-1 to match the synthetic fleet's INR pricing and Asia/Kolkata timezone."
  type        = string
  default     = "ap-south-1"
}

variable "environment" {
  description = "Environment name, used in resource names and tags."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging or prod."
  }
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "availability_zone_count" {
  description = "AZs to spread across. Three, because Kafka with replication factor 3 needs three failure domains to mean anything."
  type        = number
  default     = 3

  validation {
    condition     = var.availability_zone_count >= 3
    error_message = "At least 3 AZs: replication factor 3 across 2 AZs is not fault tolerant."
  }
}

variable "eks_version" {
  type    = string
  default = "1.31"
}

variable "node_instance_types" {
  description = "Node types. Smallest viable, per the cost guardrails in CLAUDE.md S13."
  type        = list(string)
  default     = ["t3.large"]
}

variable "node_desired_size" {
  type    = number
  default = 3
}

variable "node_max_size" {
  type    = number
  default = 12
}

variable "use_spot_instances" {
  description = "Spot for the processor pool. Safe here: consumers are stateless and a lost pod is a rebalance, not data loss."
  type        = bool
  default     = true
}

variable "kafka_broker_count" {
  type    = number
  default = 3
}

variable "kafka_instance_type" {
  type    = string
  default = "kafka.m5.large"
}

variable "kafka_partitions" {
  description = "Default partitions for telemetry.raw. 96 from a measurement, not a guess - see ADR-0004."
  type        = number
  default     = 96
}

variable "postgres_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "postgres_allocated_storage_gb" {
  type    = number
  default = 100
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "monthly_budget_usd" {
  description = "Hard budget alarm. A single developer on a personal account needs this to be real."
  type        = number
  default     = 50
}

variable "budget_alert_email" {
  description = "Where budget alerts go. Empty disables the notification, not the budget."
  type        = string
  default     = ""
}

variable "allowed_admin_cidrs" {
  description = "CIDRs permitted to reach the EKS public endpoint. Never 0.0.0.0/0 - see the validation."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.allowed_admin_cidrs, "0.0.0.0/0")
    error_message = "0.0.0.0/0 would expose the Kubernetes API to the internet. Name your own address."
  }
}
