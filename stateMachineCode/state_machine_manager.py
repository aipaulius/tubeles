import boto3
import sagemaker
import time
import random
import uuid
import logging
import stepfunctions
import io
import random
import json
import sys

from sagemaker.amazon.amazon_estimator import get_image_uri
from stepfunctions import steps
from stepfunctions.steps import TrainingStep, ModelStep, TransformStep
from stepfunctions.inputs import ExecutionInput
from stepfunctions.workflow import Workflow
from stepfunctions.template import TrainingPipeline
from stepfunctions.template.utils import replace_parameters_with_jsonpath

stepfunctions.set_stream_logger(level=logging.INFO)

region = boto3.Session().region_name

# Create a schema for input
event_input = ExecutionInput(schema={
    'BuildId': str,
    'Job': str,
    'Model': str,
    'Endpoint': str,
    'ecrArn': str,
    'dataBucketPath': str,
    'authorDate': str,
    'DynamoDBTable': str,
    'triggerSource': str,
    'commitId': str,
})

# Define static variables determined by appsec
sagemaker_role = '[SageMakerRole]'
workflow_role = '[StepFunctionsRole]'
ecr_Arn = 'latest'
state_machine_arn = '[trainingStateMachine]'
state_machine_name = '[trainingStateMachineName]'
dynamoDBTable = '[dynamoDBTable]'
endpoint_wait_lambda = '[endpointWaitLambda]'
model_test_step = '[modelTestLambda]'
model_artifact_bucket = 's3://[modelArtifactBucket]/'
kms_key = '[kmsKey]'

# Create a retry configuration for SageMaker throttling exceptions. This is attached to
# the SageMaker steps to ensure they are retried until they run.
SageMaker_throttling_retry = stepfunctions.steps.states.Retry(
    error_equals=['ThrottlingException', 'SageMaker.AmazonSageMakerException'],
    interval_seconds=5,
    max_attempts=60,
    backoff_rate=1.25
)

# Create an estimator with training specifications
custom_estimator = sagemaker.estimator.Estimator(
    ecr_Arn,
    sagemaker_role,
    train_instance_count = 1,
    train_instance_type = 'ml.m5.2xlarge',
    train_volume_size = 10,
    output_path=model_artifact_bucket,
    volume_kms_key=kms_key
)

# Create a step to train the model
training_step = steps.TrainingStep(
    'Train step',
    estimator=custom_estimator,
    data={
        'training': sagemaker.inputs.TrainingInput(event_input['dataBucketPath'], content_type='csv')
    },
    job_name=event_input['Job'],
    result_path='$.train_step_result'
)
# Add a retry configuration to the training_step
training_step.add_retry(SageMaker_throttling_retry)

# Create a step to save the model
model_step = steps.ModelStep(
    'Save model',
    model=training_step.get_expected_model(),
    model_name=event_input['Model'],
    result_path='$.save_step_result'
)
# Add a retry configuration to the model_step
model_step.add_retry(SageMaker_throttling_retry)

# Create a step to input model data into the model artifact store in DynamoDB
register_artifact_step = steps.service.DynamoDBPutItemStep(
    "Insert artifact into catalog",
    parameters={
            "Item": {
                "RunId": {
                    "S": event_input['Model']
                },
                "authorDate": {
                    "S": event_input['authorDate']
                },
                "commitId": {
                    "S": event_input['commitId']
                },
                "JobId": {
                    "S": event_input['Job']
                },
                "trainingDataObjectPath": {
                    "S": event_input['dataBucketPath']
                },
                "modelArtifactObjectPath": {
                    "S.$": "States.Format('{}', $.train_step_result.ModelArtifacts.S3ModelArtifacts)"
                },
                "ecrImageTag": {
                    "S": event_input['BuildId']
                },
                "trainingStartTime": {
                    "S.$": "States.Format('{}', $.train_step_result.TrainingStartTime)"
                },
                "trainingEndTime": {
                    "S.$": "States.Format('{}', $.train_step_result.TrainingEndTime)"
                },
                "endpointName": {
                    "S": event_input['Endpoint']
                },
                "triggerSource": {
                    "S": event_input['triggerSource']
                },
                "Accuracy": {
                    "N": "0"
                }
            },
            "TableName": dynamoDBTable
        },
        result_path='$.register_artifact_step_result'
)

# Create a step to generate an Amazon SageMaker endpoint configuration
endpoint_config_step = steps.EndpointConfigStep(
    "Create Endpoint configuration",
    endpoint_config_name=event_input['Model'],
    model_name=event_input['Model'],
    initial_instance_count=1,
    instance_type='ml.m5.large',
    result_path='$.endpoint_config_step_result'
)
# Add a retry configuration to the endpoint_config_step
endpoint_config_step.add_retry(SageMaker_throttling_retry)

# Create a step to generate an Amazon SageMaker endpoint
endpoint_step = steps.EndpointStep(
    "Create Endpoint",
    endpoint_name=event_input['Endpoint'],
    endpoint_config_name=event_input['Model'],
    result_path='$.endpoint_step_result'
)
# Add a retry configuration to the endpoint_step
endpoint_step.add_retry(SageMaker_throttling_retry)

# Create a step that triggers an AWS Lambda function that tests if the endpoint is InService
endpoint_wait_step = steps.LambdaStep(
    "Test Endpoint in service",
    parameters={
        "FunctionName": endpoint_wait_lambda,
        "Payload":{
            "Input.$":"$"
        }
    },
    result_path='$.endpoint_wait_step_result'
)


# Create a retry configuration for the endpoint_wait_step
endpoint_wait_step_retry = stepfunctions.steps.states.Retry(
    error_equals=['NotInService'],
    interval_seconds=15,
    max_attempts=30,
    backoff_rate=1.25
)
# Add a retry configuration to the endpoint_wait_step
endpoint_wait_step.add_retry(endpoint_wait_step_retry)


# Create a step that triggers an AWS Lambda function to test model accuracy and 
# update the artifact store to reflect the accuracy
model_test_step = steps.LambdaStep(
    "Test model",
    parameters={
        "FunctionName": model_test_step,
        "Payload":{
            "Input.$":"$"
        }
    },
    result_path='$.model_test_step_result'
)


# Chain the steps together to generate a full Amazon Step Functions
workflow_definition = steps.Chain([
    training_step,
    model_step,
    register_artifact_step,
    endpoint_config_step,
    endpoint_step,
    endpoint_wait_step,
    model_test_step
])


# Create a Amazon Step Functions workflow based in inputs
workflow = Workflow(
    name=state_machine_name,
    state_machine_arn=state_machine_arn,
    definition=workflow_definition,
    role=workflow_role,
    execution_input=event_input
)

# Manually update some settings that are not generated correctly by the AWS Step Functions Data Science SDK
jsonDef = workflow.definition.to_json(pretty=True)
jsonDef = jsonDef.replace("TrainingImage\": \"latest", "TrainingImage.$\": \"$$.Execution.Input['ecrArn']")
jsonDef = jsonDef.replace("Image\": \"latest", "Image.$\": \"$$.Execution.Input['ecrArn']")
jsonDef = jsonDef.replace("ModelDataUrl.$\": \"$['ModelArtifacts']['S3ModelArtifacts']", "ModelDataUrl.$\": \"$['train_step_result']['ModelArtifacts']['S3ModelArtifacts']")

# Print the AWS Step Functions definition to the log
print(state_machine_arn)
print('---------')
print(jsonDef)
print('---------')
client = boto3.client('stepfunctions')

# Update the AWS Step Function using Boto3
try:
    response = client.update_state_machine(
            stateMachineArn=state_machine_arn,
            definition=jsonDef,
        )
    print(response)
except:
    e = sys.exc_info()[0]
    f = sys.exc_info()[1]
    g = sys.exc_info()[2]
    print("error (read error): "+str(e) + str(f) + str(g))

# Update the AWS Step Functions using the AWS Step Functions Data Science SDK
# This does not work because of the 3 manual replacements made above for our use case
# workflow.update(workflow.definition, workflow_role)
