# Networking. Private subnets for everything that holds data, public only for the load
# balancer - so no database or broker has a route from the internet at all.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, var.availability_zone_count)
  name = "fleet-${var.environment}"
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

resource "aws_subnet" "private" {
  count = var.availability_zone_count

  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index)

  tags = {
    Name = "${local.name}-private-${local.azs[count.index]}"
    # Required by the AWS load balancer controller to place internal LBs.
    "kubernetes.io/role/internal-elb" = "1"
  }
}

resource "aws_subnet" "public" {
  count = var.availability_zone_count

  vpc_id                  = aws_vpc.main.id
  availability_zone       = local.azs[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index + 8)
  map_public_ip_on_launch = false

  tags = {
    Name                     = "${local.name}-public-${local.azs[count.index]}"
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

# ONE NAT gateway, not one per AZ. This is a deliberate cost/availability trade: three NAT
# gateways is roughly 3x the ~$32/month base cost, and on a personal account with a $50
# budget that is the single largest line item. The consequence is honest - losing this AZ
# costs outbound internet for private subnets until it recovers - and it is the wrong trade
# for production, where var.environment == "prod" should provision one per AZ.
resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.name}-nat" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.main]

  tags = { Name = local.name }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "public" {
  count          = var.availability_zone_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = var.availability_zone_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# --- security groups -------------------------------------------------------

resource "aws_security_group" "cluster" {
  name        = "${local.name}-cluster"
  description = "EKS control plane and nodes"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "All outbound; needed for image pulls and AWS APIs"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-cluster" }
}

# One SG per data store, each admitting ONLY the cluster SG. No CIDR-based rule reaches any
# of them, so a misconfigured subnet cannot accidentally expose a database.
resource "aws_security_group" "postgres" {
  name        = "${local.name}-postgres"
  description = "PostgreSQL; reachable only from the cluster"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "PostgreSQL from cluster workloads only"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.cluster.id]
  }

  tags = { Name = "${local.name}-postgres" }
}

resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "Redis; reachable only from the cluster"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.cluster.id]
  }

  tags = { Name = "${local.name}-redis" }
}

resource "aws_security_group" "kafka" {
  name        = "${local.name}-kafka"
  description = "Kafka; reachable only from the cluster"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "TLS listener"
    from_port       = 9094
    to_port         = 9096
    protocol        = "tcp"
    security_groups = [aws_security_group.cluster.id]
  }

  tags = { Name = "${local.name}-kafka" }
}
