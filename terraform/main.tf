terraform {
  required_version = ">= 1.6.0"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 5.0" } }
}
provider "aws" { region = var.region }

variable "region"   { type = string, default = "ap-south-1" }
variable "app_name" { type = string, default = "free-tier-poc" }
variable "ecr_repo" { type = string, default = "free-tier-poc-repo" }

# ECR
resource "aws_ecr_repository" "app" {
  name = var.ecr_repo
  image_scanning_configuration { scan_on_push = true }
}

# Default VPC + your IP
data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" { filter { name = "vpc-id" values = [data.aws_vpc.default.id] } }
data "http" "myip" { url = "https://checkip.amazonaws.com/" }

resource "aws_security_group" "app" {
  name   = "${var.app_name}-sg"
  vpc_id = data.aws_vpc.default.id
  ingress {
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = ["${chomp(data.http.myip.response_body)}/32"]
  }
  egress { from_port = 0 to_port = 0 protocol = "-1" cidr_blocks = ["0.0.0.0/0"] }
}

# ECS cluster
resource "aws_ecs_cluster" "this" { name = "${var.app_name}-cluster" }

# IAM for ECS instance profile (agent)
data "aws_iam_policy_document" "ec2_trust" {
  statement { actions = ["sts:AssumeRole"], principals { type = "Service" identifiers = ["ec2.amazonaws.com"] } }
}
resource "aws_iam_role" "ecs_instance" {
  name               = "${var.app_name}-ecs-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}
resource "aws_iam_role_policy_attachment" "ecs_instance_attach" {
  role       = aws_iam_role.ecs_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}
resource "aws_iam_instance_profile" "ecs_instance" {
  name = "${var.app_name}-ecs-prof"
  role = aws_iam_role.ecs_instance.name
}

# ECS task execution role
data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement { actions = ["sts:AssumeRole"], principals { type = "Service" identifiers = ["ecs-tasks.amazonaws.com"] } }
}
resource "aws_iam_role" "ecs_task_exec" {
  name               = "${var.app_name}-task-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}
resource "aws_iam_role_policy_attachment" "ecs_task_exec_attach" {
  role       = aws_iam_role.ecs_task_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ECS-optimized AMI
data "aws_ssm_parameter" "ecs_ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2/recommended/image_id"
}

# EC2 capacity: t3.micro 1 instance
resource "aws_launch_template" "ecs" {
  name_prefix   = "${var.app_name}-lt-"
  image_id      = data.aws_ssm_parameter.ecs_ami.value
  instance_type = "t3.micro"
  iam_instance_profile { name = aws_iam_instance_profile.ecs_instance.name }
  vpc_security_group_ids = [aws_security_group.app.id]
  user_data = base64encode(<<EOF
#!/bin/bash
echo "ECS_CLUSTER=${aws_ecs_cluster.this.name}" >> /etc/ecs/ecs.config
EOF
  )
}
resource "aws_autoscaling_group" "ecs" {
  name                = "${var.app_name}-asg"
  desired_capacity    = 1
  max_size            = 1
  min_size            = 1
  vpc_zone_identifier = data.aws_subnets.default.ids
  launch_template { id = aws_launch_template.ecs.id, version = "$Latest" }
  lifecycle { ignore_changes = [desired_capacity] }
}

# Logs, task definition & service (EC2 launch type)
resource "aws_cloudwatch_log_group" "app" { name = "/ecs/${var.app_name}" retention_in_days = 7 }

resource "aws_ecs_task_definition" "app" {
  family                   = "${var.app_name}-task"
  network_mode             = "awsvpc"
  requires_compatibilities = ["EC2"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_task_exec.arn
  container_definitions = jsonencode([{
    name  = "web",
    image = "${aws_ecr_repository.app.repository_url}:latest",
    essential = true,
    portMappings = [{ containerPort = 3000, hostPort = 3000, protocol = "tcp" }],
    logConfiguration = { logDriver = "awslogs", options = {
      awslogs-group = aws_cloudwatch_log_group.app.name,
      awslogs-region = var.region,
      awslogs-stream-prefix = "ecs" } }
  }])
}

resource "aws_ecs_service" "app" {
  name            = "${var.app_name}-service"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 1
  launch_type     = "EC2"
  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = true
  }
}

output "cluster" { value = aws_ecs_cluster.this.name }
output "service" { value = aws_ecs_service.app.name }
