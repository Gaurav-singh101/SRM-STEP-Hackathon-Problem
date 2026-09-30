# EKS cluster and node groups.
#
# Two node groups on purpose: the processor is stateless and a lost pod is a consumer-group
# rebalance rather than data loss, so it can run on spot. The API and batch jobs run on
# on-demand, because an interrupted API pod is a user-visible error and an interrupted
# migration is worse.

resource "aws_iam_role" "cluster" {
  name = "${local.name}-cluster"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_kms_key" "eks" {
  description             = "EKS envelope encryption for Kubernetes Secrets"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

resource "aws_eks_cluster" "main" {
  name     = local.name
  role_arn = aws_iam_role.cluster.arn
  version  = var.eks_version

  vpc_config {
    subnet_ids              = concat(aws_subnet.private[*].id, aws_subnet.public[*].id)
    security_group_ids      = [aws_security_group.cluster.id]
    endpoint_private_access = true
    # Public access only from named addresses. The variable validation refuses 0.0.0.0/0,
    # and an empty list means private-only - which is the safe default, not an oversight.
    endpoint_public_access = length(var.allowed_admin_cidrs) > 0
    public_access_cidrs    = var.allowed_admin_cidrs
  }

  # Envelope-encrypt Secrets with a customer-managed key. Without this, etcd encryption uses
  # an AWS-managed key and "our own key" is not a claim that can be made.
  encryption_config {
    provider {
      key_arn = aws_kms_key.eks.arn
    }
    resources = ["secrets"]
  }

  # Audit logging on. STRIDE R4 is unattributable administrative action, and this is the
  # control that answers it.
  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  access_config {
    authentication_mode = "API"
  }

  depends_on = [aws_iam_role_policy_attachment.cluster]

  tags = { Name = local.name }
}

# --- node role -------------------------------------------------------------

resource "aws_iam_role" "node" {
  name = "${local.name}-node"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])

  role       = aws_iam_role.node.name
  policy_arn = each.value
}

# Read ONLY the two secrets this platform owns, named explicitly. A wildcard on
# secretsmanager:* would let any compromised pod read every secret in the account.
resource "aws_iam_role_policy" "node_secrets" {
  name = "${local.name}-read-own-secrets"
  role = aws_iam_role.node.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      Resource = [aws_secretsmanager_secret.postgres.arn]
    }]
  })
}

# --- node groups -----------------------------------------------------------

resource "aws_eks_node_group" "system" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "system"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private[*].id

  instance_types = var.node_instance_types
  capacity_type  = "ON_DEMAND"

  scaling_config {
    desired_size = var.node_desired_size
    min_size     = 2
    max_size     = var.node_max_size
  }

  update_config {
    max_unavailable = 1
  }

  labels = { workload = "system" }

  depends_on = [aws_iam_role_policy_attachment.node]

  tags = { Name = "${local.name}-system" }
}

resource "aws_eks_node_group" "processor" {
  count = var.use_spot_instances ? 1 : 0

  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "processor-spot"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private[*].id

  # Spot, and safe here specifically: a terminated consumer triggers a cooperative-sticky
  # rebalance, offsets were committed only after the last batch was durable, and delivery is
  # at-least-once with idempotent writes. Losing a pod costs a reprocessed batch, not data.
  instance_types = ["t3.large", "t3a.large", "m5.large"]
  capacity_type  = "SPOT"

  scaling_config {
    desired_size = var.node_desired_size
    min_size     = 0
    max_size     = var.node_max_size
  }

  labels = { workload = "processor" }

  # Tainted so only the processor tolerates it. Without this the API would schedule onto
  # spot capacity and a reclaim would become a user-visible 502.
  taint {
    key    = "workload"
    value  = "processor"
    effect = "NO_SCHEDULE"
  }

  depends_on = [aws_iam_role_policy_attachment.node]

  tags = { Name = "${local.name}-processor-spot" }
}

# --- cost guardrail --------------------------------------------------------
#
# A budget is not optional on a personal account. CLAUDE.md S13 asks for a budget alarm and
# a teardown script, and the alarm is the one that prevents a surprise.

resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # 50% first, not 100%. An alert at the limit tells you it is already spent.
  dynamic "notification" {
    for_each = var.budget_alert_email == "" ? [] : [50, 80, 100]

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }
}
