// Jade reference server — Azure Container Apps deployment.
// Validate before use:  az bicep build --file azure/container-apps.bicep
//
// Deploys: Log Analytics workspace, Container Apps environment, and the
// jade-server container app (image built from server/Dockerfile and
// pushed to a registry first — see server/README.md).
//
// Usage:
//   az group create -n jade-rg -l eastus
//   az deployment group create -g jade-rg -f azure/container-apps.bicep \
//     -p containerImage=<registry>/jade-server:latest \
//        tokenKey=<base64-32-bytes> chatApiKey=<key>

targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = 'eastus'

@description('Container image for jade-server (registry/repo:tag).')
param containerImage string

@description('Base64 Fernet key for connector-token encryption (openssl rand -base64 32).')
@secure()
param tokenKey string

@description('API key for the hosted chat provider (optional; enables /v1/chat/completions).')
@secure()
param chatApiKey string = ''

@description('Public URL of the deployment (for OAuth redirect URIs).')
param publicUrl string = ''

@description('Google OAuth client id (optional).')
param googleClientId string = ''
@secure()
@description('Google OAuth client secret (optional).')
param googleClientSecret string = ''

@description('Microsoft (Entra) OAuth client id (optional).')
param microsoftClientId string = ''
@secure()
@description('Microsoft (Entra) OAuth client secret (optional).')
param microsoftClientSecret string = ''

@description('GitHub OAuth client id (optional).')
param githubClientId string = ''
@secure()
@description('GitHub OAuth client secret (optional).')
param githubClientSecret string = ''

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: 'jade-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: 'jade-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: 'jade-server'
  location: location
  properties: {
    managedEnvironmentId: appEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http' // TLS is terminated by Container Apps at the edge.
      }
      secrets: [
        { name: 'token-key', value: tokenKey }
        { name: 'chat-api-key', value: chatApiKey }
        { name: 'google-client-secret', value: googleClientSecret }
        { name: 'microsoft-client-secret', value: microsoftClientSecret }
        { name: 'github-client-secret', value: githubClientSecret }
      ]
    }
    template: {
      containers: [
        {
          name: 'jade-server'
          image: containerImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'JADE_SERVER_HOST', value: '0.0.0.0' }
            { name: 'JADE_SERVER_PORT', value: '8080' }
            { name: 'JADE_DATA_DIR', value: '/data' }
            { name: 'JADE_TOKEN_KEY', secretRef: 'token-key' }
            { name: 'JADE_CHAT_API_KEY', secretRef: 'chat-api-key' }
            { name: 'JADE_PUBLIC_URL', value: publicUrl }
            { name: 'GOOGLE_CLIENT_ID', value: googleClientId }
            { name: 'GOOGLE_CLIENT_SECRET', secretRef: 'google-client-secret' }
            { name: 'MICROSOFT_CLIENT_ID', value: microsoftClientId }
            { name: 'MICROSOFT_CLIENT_SECRET', secretRef: 'microsoft-client-secret' }
            { name: 'GITHUB_CLIENT_ID', value: githubClientId }
            { name: 'GITHUB_CLIENT_SECRET', secretRef: 'github-client-secret' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8080 }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

output fqdn string = containerApp.properties.configuration.ingress.fqdn
