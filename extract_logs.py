import boto3
import json
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import seaborn as sns
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

# Load environment variables from .env file
load_dotenv()

# Initialize AWS clients with credentials from environment
aws_access_key = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_key = os.getenv('AWS_SECRET_ACCESS_KEY')
aws_region = os.getenv('AWS_DEFAULT_REGION', 'us-west-2')

# Initialize multiple AWS service clients
cloudwatch = boto3.client('cloudwatch', aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key, region_name=aws_region)
ce = boto3.client('ce', aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key, region_name=aws_region)
ec2 = boto3.client('ec2', aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key, region_name=aws_region)
rds = boto3.client('rds', aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key, region_name=aws_region)
lambda_client = boto3.client('lambda', aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key, region_name=aws_region)
s3 = boto3.client('s3', aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key, region_name=aws_region)
logs_client = boto3.client('logs', aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key, region_name=aws_region)
elbv2 = boto3.client('elbv2', aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key, region_name=aws_region)
sagemaker = boto3.client('sagemaker', aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key, region_name=aws_region)
bedrock = boto3.client('bedrock', aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key, region_name=aws_region)
bedrock_runtime = boto3.client('bedrock-runtime', aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key, region_name=aws_region)

def get_cloudwatch_metrics(namespace, metric_name, start_time, end_time):
    """Extract CloudWatch metrics"""
    try:
        response = cloudwatch.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,
            Statistics=['Average', 'Sum', 'Maximum', 'Minimum']
        )
        return response['Datapoints']
    except Exception as e:
        print(f"Error fetching CloudWatch metrics: {e}")
        return []

def get_comprehensive_cloudwatch_metrics(start_time, end_time):
    """Extract comprehensive CloudWatch metrics from multiple services"""
    metrics_data = {}
    
    # Define metrics to extract for various AWS services
    metrics_config = {
        'AWS/EC2': ['CPUUtilization', 'NetworkIn', 'NetworkOut', 'DiskReadOps', 'DiskWriteOps'],
        'AWS/RDS': ['CPUUtilization', 'DatabaseConnections', 'ReadIOPS', 'WriteIOPS'],
        'AWS/Lambda': ['Invocations', 'Duration', 'Errors', 'Throttles'],
        'AWS/S3': ['BucketSizeBytes', 'NumberOfObjects'],
        'AWS/ApplicationELB': ['TargetResponseTime', 'RequestCount', 'HTTPCode_Target_2XX_Count'],
        'AWS/EBS': ['VolumeReadOps', 'VolumeWriteOps', 'VolumeTotalReadTime', 'VolumeTotalWriteTime'],
        'AWS/CloudFront': ['Requests', 'BytesDownloaded', 'BytesUploaded'],
        'AWS/ApiGateway': ['Count', 'Latency', '4XXError', '5XXError'],
        'AWS/SageMaker': ['InvocationsPerInstance', 'ModelLatency', 'OverheadLatency', 'InvocationFailures'],
        'AWS/Bedrock': ['InvocationLatency', 'Invocations', 'InvocationClientError', 'InvocationServerError']
    }
    
    for namespace, metrics in metrics_config.items():
        metrics_data[namespace] = {}
        for metric_name in metrics:
            try:
                response = cloudwatch.get_metric_statistics(
                    Namespace=namespace,
                    MetricName=metric_name,
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=3600,
                    Statistics=['Average', 'Sum', 'Maximum', 'Minimum']
                )
                metrics_data[namespace][metric_name] = response['Datapoints']
            except Exception as e:
                print(f"Error fetching {namespace} {metric_name}: {e}")
                metrics_data[namespace][metric_name] = []
    
    return metrics_data

def get_resource_inventory():
    """Get inventory of AWS resources"""
    inventory = {}
    
    try:
        # EC2 Instances
        ec2_response = ec2.describe_instances()
        instances = []
        for reservation in ec2_response['Reservations']:
            for instance in reservation['Instances']:
                instances.append({
                    'InstanceId': instance['InstanceId'],
                    'InstanceType': instance['InstanceType'],
                    'State': instance['State']['Name'],
                    'LaunchTime': instance.get('LaunchTime', '').isoformat() if instance.get('LaunchTime') else ''
                })
        inventory['ec2_instances'] = instances
    except Exception as e:
        print(f"Error fetching EC2 instances: {e}")
        inventory['ec2_instances'] = []
    
    try:
        # RDS Instances
        rds_response = rds.describe_db_instances()
        db_instances = []
        for db in rds_response['DBInstances']:
            db_instances.append({
                'DBInstanceIdentifier': db['DBInstanceIdentifier'],
                'DBInstanceClass': db['DBInstanceClass'],
                'Engine': db['Engine'],
                'DBInstanceStatus': db['DBInstanceStatus']
            })
        inventory['rds_instances'] = db_instances
    except Exception as e:
        print(f"Error fetching RDS instances: {e}")
        inventory['rds_instances'] = []
    
    try:
        # Lambda Functions
        lambda_response = lambda_client.list_functions()
        functions = []
        for func in lambda_response['Functions']:
            functions.append({
                'FunctionName': func['FunctionName'],
                'Runtime': func['Runtime'],
                'MemorySize': func['MemorySize'],
                'LastModified': func['LastModified']
            })
        inventory['lambda_functions'] = functions
    except Exception as e:
        print(f"Error fetching Lambda functions: {e}")
        inventory['lambda_functions'] = []
    
    try:
        # S3 Buckets
        s3_response = s3.list_buckets()
        buckets = []
        for bucket in s3_response['Buckets']:
            buckets.append({
                'Name': bucket['Name'],
                'CreationDate': bucket['CreationDate'].isoformat()
            })
        inventory['s3_buckets'] = buckets
    except Exception as e:
        print(f"Error fetching S3 buckets: {e}")
        inventory['s3_buckets'] = []
    
    try:
        # SageMaker Endpoints
        sagemaker_response = sagemaker.list_endpoints()
        endpoints = []
        for endpoint in sagemaker_response['Endpoints']:
            endpoints.append({
                'EndpointName': endpoint['EndpointName'],
                'EndpointStatus': endpoint['EndpointStatus'],
                'CreationTime': endpoint['CreationTime'].isoformat(),
                'InstanceType': endpoint.get('InstanceType', 'Unknown')
            })
        inventory['sagemaker_endpoints'] = endpoints
    except Exception as e:
        print(f"Error fetching SageMaker endpoints: {e}")
        inventory['sagemaker_endpoints'] = []
    
    try:
        # SageMaker Models
        models_response = sagemaker.list_models()
        models = []
        for model in models_response['Models']:
            models.append({
                'ModelName': model['ModelName'],
                'CreationTime': model['CreationTime'].isoformat()
            })
        inventory['sagemaker_models'] = models
    except Exception as e:
        print(f"Error fetching SageMaker models: {e}")
        inventory['sagemaker_models'] = []
    
    try:
        # Bedrock Foundation Models
        bedrock_response = bedrock.list_foundation_models()
        foundation_models = []
        for model in bedrock_response['modelSummaries']:
            foundation_models.append({
                'ModelId': model['modelId'],
                'ModelName': model.get('modelName', 'Unknown'),
                'ProviderName': model.get('providerName', 'Unknown'),
                'InputModalities': model.get('inputModalities', []),
                'OutputModalities': model.get('outputModalities', [])
            })
        inventory['bedrock_foundation_models'] = foundation_models[:10]  # Limit to first 10
    except Exception as e:
        print(f"Error fetching Bedrock models: {e}")
        inventory['bedrock_foundation_models'] = []
    
    return inventory

def get_cost_and_usage(start_date, end_date):
    """Extract cost and usage information"""
    try:
        response = ce.get_cost_and_usage(
            TimePeriod={
                'Start': start_date,
                'End': end_date
            },
            Granularity='DAILY',
            Metrics=['UnblendedCost', 'UsageQuantity'],
            GroupBy=[
                {'Type': 'DIMENSION', 'Key': 'SERVICE'},
                {'Type': 'DIMENSION', 'Key': 'REGION'}
            ]
        )
        return response['ResultsByTime']
    except Exception as e:
        print(f"Error fetching cost and usage: {e}")
        return []

def get_billing_info():
    """Extract billing information"""
    try:
        response = ce.get_cost_and_usage(
            TimePeriod={
                'Start': (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%d'),
                'End': datetime.now(timezone.utc).strftime('%Y-%m-%d')
            },
            Granularity='MONTHLY',
            Metrics=['UnblendedCost'],
            Filter={
                'Dimensions': {'Key': 'PURCHASE_TYPE', 'Values': ['On Demand', 'Reserved']}
            }
        )
        return response['ResultsByTime']
    except Exception as e:
        print(f"Error fetching billing info: {e}")
        return []

def get_comprehensive_cost_analysis(start_date, end_date):
    """Get comprehensive cost analysis with multiple dimensions"""
    cost_analysis = {}
    
    # Cost by Service
    try:
        response = ce.get_cost_and_usage(
            TimePeriod={'Start': start_date, 'End': end_date},
            Granularity='DAILY',
            Metrics=['UnblendedCost', 'BlendedCost'], 
            GroupBy=[{'Type': 'DIMENSION', 'Key': 'SERVICE'}]
        )
        cost_analysis['by_service'] = response['ResultsByTime']
    except Exception as e:
        print(f"Error fetching cost by service: {e}")
        cost_analysis['by_service'] = []
    
    # Cost by Region  
    try:
        response = ce.get_cost_and_usage(
            TimePeriod={'Start': start_date, 'End': end_date},
            Granularity='DAILY',
            Metrics=['UnblendedCost'],
            GroupBy=[{'Type': 'DIMENSION', 'Key': 'REGION'}]
        )
        cost_analysis['by_region'] = response['ResultsByTime']
    except Exception as e:
        print(f"Error fetching cost by region: {e}")
        cost_analysis['by_region'] = []
    
    return cost_analysis

def get_enhanced_billing_info():
    """Extract comprehensive billing information"""
    billing_info = {}
    
    # Monthly costs
    try:
        response = ce.get_cost_and_usage(
            TimePeriod={
                'Start': (datetime.now(timezone.utc) - timedelta(days=90)).strftime('%Y-%m-%d'),
                'End': datetime.now(timezone.utc).strftime('%Y-%m-%d')
            },
            Granularity='MONTHLY',
            Metrics=['UnblendedCost', 'BlendedCost']
        )
        billing_info['monthly_costs'] = response['ResultsByTime'] 
    except Exception as e:
        print(f"Error fetching monthly billing: {e}")
        billing_info['monthly_costs'] = []
    
    # Current month forecast
    try:
        response = ce.get_cost_forecast(
            TimePeriod={
                'Start': datetime.now(timezone.utc).strftime('%Y-%m-%d'), 
                'End': (datetime.now(timezone.utc) + timedelta(days=30)).strftime('%Y-%m-%d')
            },
            Metric='UNBLENDED_COST',
            Granularity='MONTHLY'
        )
        billing_info['cost_forecast'] = response['ForecastResultsByTime']
    except Exception as e:
        print(f"Error fetching cost forecast: {e}")
        billing_info['cost_forecast'] = []
    
    return billing_info

def create_visualizations(data, output_dir):
    """Create comprehensive visualizations from the extracted data"""
    plt.style.use('default')
    
    # Set up the figure with subplots
    fig = plt.figure(figsize=(20, 24))
    
    # 1. Cost trend over time
    if data.get('cost_analysis', {}).get('by_service', []):
        ax1 = plt.subplot(4, 2, 1)
        daily_costs = {}
        try:
            for day_data in data['cost_analysis']['by_service']:
                if 'TimePeriod' in day_data and 'Total' in day_data:
                    date = day_data['TimePeriod']['Start']
                    total_cost = float(day_data['Total'].get('UnblendedCost', {}).get('Amount', 0))
                    daily_costs[date] = total_cost
            
            if daily_costs:
                dates = list(daily_costs.keys())
                costs = list(daily_costs.values())
                ax1.plot(dates, costs, marker='o')
                ax1.set_title('Daily Cost Trend', fontsize=14, fontweight='bold')
                ax1.set_xlabel('Date')
                ax1.set_ylabel('Cost ($)')
                ax1.tick_params(axis='x', rotation=45)
            else:
                ax1.text(0.5, 0.5, 'No cost data available', ha='center', va='center', transform=ax1.transAxes)
                ax1.set_title('Daily Cost Trend (No Data)', fontsize=14, fontweight='bold')
        except Exception as e:
            print(f"Error creating cost trend chart: {e}")
            ax1.text(0.5, 0.5, f'Error: {str(e)}', ha='center', va='center', transform=ax1.transAxes)
            ax1.set_title('Daily Cost Trend (Error)', fontsize=14, fontweight='bold')
    else:
        ax1 = plt.subplot(4, 2, 1)
        ax1.text(0.5, 0.5, 'No cost data available', ha='center', va='center', transform=ax1.transAxes)
        ax1.set_title('Daily Cost Trend (No Data)', fontsize=14, fontweight='bold')
    
    # 2. Service cost breakdown (Pie chart)
    if data.get('cost_analysis', {}).get('by_service', []):
        ax2 = plt.subplot(4, 2, 2)
        service_costs = defaultdict(float)
        try:
            for day_data in data['cost_analysis']['by_service']:
                if 'Groups' in day_data:
                    for group in day_data['Groups']:
                        if 'Keys' in group and group['Keys']:
                            service = group['Keys'][0]
                            cost = float(group.get('Metrics', {}).get('UnblendedCost', {}).get('Amount', 0))
                            service_costs[service] += cost
            
            if service_costs and any(cost > 0 for cost in service_costs.values()):
                services = list(service_costs.keys())[:10]  # Top 10 services
                costs = [service_costs[s] for s in services if service_costs[s] > 0]
                services = [s for s in services if service_costs[s] > 0]
                
                if services and costs:
                    ax2.pie(costs, labels=services, autopct='%1.1f%%')
                    ax2.set_title('Cost by Service (Top 10)', fontsize=14, fontweight='bold')
                else:
                    ax2.text(0.5, 0.5, 'No service cost data', ha='center', va='center', transform=ax2.transAxes)
                    ax2.set_title('Cost by Service (No Data)', fontsize=14, fontweight='bold')
            else:
                ax2.text(0.5, 0.5, 'No service cost data', ha='center', va='center', transform=ax2.transAxes)
                ax2.set_title('Cost by Service (No Data)', fontsize=14, fontweight='bold')
        except Exception as e:
            print(f"Error creating service cost chart: {e}")
            ax2.text(0.5, 0.5, f'Error: {str(e)}', ha='center', va='center', transform=ax2.transAxes)
            ax2.set_title('Cost by Service (Error)', fontsize=14, fontweight='bold')
    else:
        ax2 = plt.subplot(4, 2, 2)
        ax2.text(0.5, 0.5, 'No cost data available', ha='center', va='center', transform=ax2.transAxes)
        ax2.set_title('Cost by Service (No Data)', fontsize=14, fontweight='bold')
    
    # 3. EC2 CPU Utilization
    if 'AWS/EC2' in data['cloudwatch_metrics'] and 'CPUUtilization' in data['cloudwatch_metrics']['AWS/EC2']:
        ax3 = plt.subplot(4, 2, 3)
        cpu_data = data['cloudwatch_metrics']['AWS/EC2']['CPUUtilization']
        if cpu_data:
            timestamps = [point['Timestamp'] for point in cpu_data]
            values = [point['Average'] for point in cpu_data]
            ax3.plot(timestamps, values, color='green', marker='o')
            ax3.set_title('EC2 CPU Utilization', fontsize=14, fontweight='bold')
            ax3.set_ylabel('CPU %')
            ax3.tick_params(axis='x', rotation=45)
    
    # 4. Resource Inventory Bar Chart
    ax4 = plt.subplot(4, 2, 4)
    inventory = data.get('resource_inventory', {})
    resource_counts = {
        'EC2': len(inventory.get('ec2_instances', [])),
        'RDS': len(inventory.get('rds_instances', [])),
        'Lambda': len(inventory.get('lambda_functions', [])),
        'S3': len(inventory.get('s3_buckets', [])),
        'SageMaker': len(inventory.get('sagemaker_endpoints', [])),
        'Bedrock Models': len(inventory.get('bedrock_foundation_models', []))
    }
    resources = list(resource_counts.keys())
    counts = list(resource_counts.values())
    
    bars = ax4.bar(resources, counts, color=['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b'])
    ax4.set_title('AWS Resource Inventory', fontsize=14, fontweight='bold')
    ax4.set_ylabel('Count')
    
    # Add value labels on bars
    for bar in bars:
        height = bar.get_height()
        ax4.text(bar.get_x() + bar.get_width()/2., height,
                f'{int(height)}', ha='center', va='bottom')
    
    # 5. Monthly cost comparison
    if data['enhanced_billing_info']['monthly_costs']:
        ax5 = plt.subplot(4, 2, 5)
        monthly_data = data['enhanced_billing_info']['monthly_costs']
        months = [item['TimePeriod']['Start'] for item in monthly_data]
        costs = [float(item['Total']['UnblendedCost']['Amount']) for item in monthly_data]
        ax5.bar(months, costs, color='skyblue')
        ax5.set_title('Monthly Cost Comparison', fontsize=14, fontweight='bold')
        ax5.set_ylabel('Cost ($)')
        ax5.tick_params(axis='x', rotation=45)
    
    # 6. Network I/O for EC2
    if 'AWS/EC2' in data['cloudwatch_metrics']:
        ax6 = plt.subplot(4, 2, 6)
        network_in = data['cloudwatch_metrics']['AWS/EC2'].get('NetworkIn', [])
        network_out = data['cloudwatch_metrics']['AWS/EC2'].get('NetworkOut', [])
        
        if network_in and network_out:
            timestamps = [point['Timestamp'] for point in network_in]
            in_values = [point['Sum']/1024/1024 for point in network_in]  # Convert to MB
            out_values = [point['Sum']/1024/1024 for point in network_out]
            
            ax6.plot(timestamps, in_values, label='Network In', color='blue', marker='o')
            ax6.plot(timestamps, out_values, label='Network Out', color='red', marker='s')
            ax6.set_title('EC2 Network I/O (MB)', fontsize=14, fontweight='bold')
            ax6.set_ylabel('MB')
            ax6.legend()
            ax6.tick_params(axis='x', rotation=45)
    
    # 7. Lambda metrics
    if 'AWS/Lambda' in data['cloudwatch_metrics']:
        ax7 = plt.subplot(4, 2, 7)
        invocations = data['cloudwatch_metrics']['AWS/Lambda'].get('Invocations', [])
        
        if invocations:
            timestamps = [point['Timestamp'] for point in invocations]
            inv_values = [point['Sum'] for point in invocations]
            
            ax7.plot(timestamps, inv_values, color='green', marker='o', label='Invocations')
            ax7.set_title('Lambda Invocations', fontsize=14, fontweight='bold')
            ax7.set_ylabel('Invocations')
            ax7.tick_params(axis='x', rotation=45)
    
    # 8. Cost forecast
    if data['enhanced_billing_info']['cost_forecast']:
        ax8 = plt.subplot(4, 2, 8)
        forecast_data = data['enhanced_billing_info']['cost_forecast']
        periods = [item['TimePeriod']['Start'] for item in forecast_data]
        mean_values = [float(item['MeanValue']) for item in forecast_data]
        
        ax8.plot(periods, mean_values, color='orange', marker='D', linewidth=2)
        ax8.set_title('Cost Forecast', fontsize=14, fontweight='bold')
        ax8.set_ylabel('Forecasted Cost ($)')
        ax8.tick_params(axis='x', rotation=45)
    
    plt.tight_layout()
    chart_path = os.path.join(output_dir, 'aws_comprehensive_dashboard.png')
    plt.savefig(chart_path, dpi=300, bbox_inches='tight')
    plt.close()
    
    return chart_path

def extract_all_metrics():
    """Main function to extract comprehensive AWS metrics, billing info, and create visualizations"""
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=7)
    
    print("🚀 Starting comprehensive AWS data extraction...")
    
    # Create output directory for this session
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    output_dir = f'aws_analysis_{timestamp}'
    os.makedirs(output_dir, exist_ok=True)
    
    print("📊 Extracting CloudWatch metrics...")
    cloudwatch_metrics = get_comprehensive_cloudwatch_metrics(start_time, end_time)
    
    print("💰 Analyzing costs and usage...")
    cost_analysis = get_comprehensive_cost_analysis(start_time.strftime('%Y-%m-%d'), end_time.strftime('%Y-%m-%d'))
    
    print("🏗️ Gathering resource inventory...")
    resource_inventory = get_resource_inventory()
    
    print("💳 Extracting billing information...")
    enhanced_billing_info = get_enhanced_billing_info()
    
    # Legacy data for backward compatibility
    print("📈 Gathering legacy format data...")
    legacy_cloudwatch = get_cloudwatch_metrics('AWS/EC2', 'CPUUtilization', start_time, end_time)
    legacy_cost_usage = get_cost_and_usage(start_time.strftime('%Y-%m-%d'), end_time.strftime('%Y-%m-%d'))
    legacy_billing = get_billing_info()
    
    results = {
        'cloudwatch_metrics': cloudwatch_metrics,
        'cost_analysis': cost_analysis,
        'resource_inventory': resource_inventory,
        'enhanced_billing_info': enhanced_billing_info,
        # Legacy format for backward compatibility
        'legacy_data': {
            'cloudwatch_metrics': legacy_cloudwatch,
            'cost_and_usage': legacy_cost_usage,
            'billing_info': legacy_billing
        },
        'extraction_metadata': {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'region': aws_region,
            'output_directory': output_dir
        }
    }
    
    print("📈 Creating visualizations...")
    try:
        chart_path = create_visualizations(results, output_dir)
        results['extraction_metadata']['chart_path'] = chart_path
    except Exception as e:
        print(f"⚠️ Error creating visualizations: {e}")
        results['extraction_metadata']['chart_path'] = None
    
    return results, output_dir

if __name__ == '__main__':
    try:
        data, output_dir = extract_all_metrics()
        
        # Save comprehensive JSON data
        json_filename = os.path.join(output_dir, 'aws_comprehensive_data.json')
        with open(json_filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, default=str)
        
        # Save summary report
        summary_filename = os.path.join(output_dir, 'summary_report.txt')
        with open(summary_filename, 'w', encoding='utf-8') as f:
            f.write("=== AWS COMPREHENSIVE ANALYSIS REPORT ===\\n\\n")
            f.write(f"Generated on: {data['extraction_metadata']['timestamp']}\\n")
            f.write(f"Analysis period: {data['extraction_metadata']['start_time']} to {data['extraction_metadata']['end_time']}\\n")
            f.write(f"Region: {data['extraction_metadata']['region']}\\n\\n")
            
            # Resource Summary
            f.write("=== RESOURCE INVENTORY ===\\n")
            f.write(f"EC2 Instances: {len(data['resource_inventory']['ec2_instances'])}\\n")
            f.write(f"RDS Instances: {len(data['resource_inventory']['rds_instances'])}\\n") 
            f.write(f"Lambda Functions: {len(data['resource_inventory']['lambda_functions'])}\\n")
            f.write(f"S3 Buckets: {len(data['resource_inventory']['s3_buckets'])}\\n")
            f.write(f"SageMaker Endpoints: {len(data['resource_inventory']['sagemaker_endpoints'])}\\n")
            f.write(f"SageMaker Models: {len(data['resource_inventory']['sagemaker_models'])}\\n")
            f.write(f"Bedrock Foundation Models: {len(data['resource_inventory']['bedrock_foundation_models'])}\\n\\n")
            
            # Metrics Summary
            f.write("=== CLOUDWATCH METRICS SUMMARY ===\\n")
            for namespace, metrics in data['cloudwatch_metrics'].items():
                f.write(f"{namespace}:\\n")
                for metric_name, datapoints in metrics.items():
                    f.write(f"  - {metric_name}: {len(datapoints)} datapoints\\n")
                f.write("\\n")
            
            # Cost Summary
            f.write("=== COST ANALYSIS SUMMARY ===\\n")
            if data.get('cost_analysis', {}).get('by_service', []):
                total_cost = 0
                service_costs = defaultdict(float)
                try:
                    for day_data in data['cost_analysis']['by_service']:
                        if 'Total' in day_data and 'UnblendedCost' in day_data['Total']:
                            total_cost += float(day_data['Total']['UnblendedCost'].get('Amount', 0))
                        if 'Groups' in day_data:
                            for group in day_data['Groups']:
                                if 'Keys' in group and group['Keys'] and 'Metrics' in group:
                                    service = group['Keys'][0]
                                    cost = float(group['Metrics'].get('UnblendedCost', {}).get('Amount', 0))
                                    service_costs[service] += cost
                    
                    f.write(f"Total Cost (7 days): ${total_cost:.2f}\\n")
                    f.write("Top Services by Cost:\\n")
                    sorted_services = sorted(service_costs.items(), key=lambda x: x[1], reverse=True)
                    for service, cost in sorted_services[:10]:
                        f.write(f"  - {service}: ${cost:.2f}\\n")
                except Exception as e:
                    f.write(f"Error processing cost data: {e}\\n")
            else:
                f.write("No cost analysis data available\\n")
        
        print("\\n" + "="*60)
        print("🎉 COMPREHENSIVE AWS ANALYSIS COMPLETE!")
        print("="*60)
        print(f"📁 Output Directory: {output_dir}")
        print(f"📊 JSON Data: {json_filename}")
        print(f"📈 Visualizations: {data['extraction_metadata'].get('chart_path', 'Not generated')}")
        print(f"📋 Summary Report: {summary_filename}")
        print("="*60)
        
        # Print quick stats
        print("\\n📊 QUICK STATISTICS:")
        inventory = data.get('resource_inventory', {})
        print(f"   • EC2 Instances: {len(inventory.get('ec2_instances', []))}")
        print(f"   • RDS Instances: {len(inventory.get('rds_instances', []))}")
        print(f"   • Lambda Functions: {len(inventory.get('lambda_functions', []))}")
        print(f"   • S3 Buckets: {len(inventory.get('s3_buckets', []))}")
        print(f"   • SageMaker Endpoints: {len(inventory.get('sagemaker_endpoints', []))}")
        print(f"   • SageMaker Models: {len(inventory.get('sagemaker_models', []))}")
        print(f"   • Bedrock Models: {len(inventory.get('bedrock_foundation_models', []))}")
        
        # Calculate total metrics collected
        total_metrics = sum(len(metrics) for metrics in data.get('cloudwatch_metrics', {}).values())
        print(f"   • CloudWatch Metrics: {total_metrics} metric types")
        
        # Cost summary with safe access
        if data.get('cost_analysis', {}).get('by_service', []):
            try:
                total_cost = 0
                for day in data['cost_analysis']['by_service']:
                    if 'Total' in day and 'UnblendedCost' in day['Total']:
                        total_cost += float(day['Total']['UnblendedCost'].get('Amount', 0))
                print(f"   • Total Cost (7 days): ${total_cost:.2f}")
            except Exception as e:
                print(f"   • Cost calculation error: {e}")
        else:
            print(f"   • Total Cost (7 days): $0.00 (No data)")
        
        print("\\n✨ Analysis complete! Check the output directory for detailed results.")
        
    except Exception as e:
        print(f"❌ Error during extraction: {e}")
        print("Please check your AWS credentials and permissions.")