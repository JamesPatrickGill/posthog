import { createClient as createClickHouseClient } from '@clickhouse/client'
import { Client, Connection, ScheduleAlreadyRunning, ScheduleOverlapPolicy } from '@temporalio/client'
import { DataConverter } from '@temporalio/common'
import { NativeConnection, Worker } from '@temporalio/worker'
import * as fs from 'fs/promises'

import { InternalFetchService } from '~/common/services/internal-fetch'
import { EncryptionCodec } from '~/common/temporal/codec'
import { PostgresRouter } from '~/common/utils/db/postgres'
import { logger } from '~/common/utils/logger'
import { PubSub } from '~/common/utils/pubsub'
import {
    HealthCheckResult,
    HealthCheckResultError,
    HealthCheckResultOk,
    PluginServerService,
    PluginsServerConfig,
} from '~/types'

import { EmailReputationService } from '../email-reputation.service'
import { createActivities } from './activities'

export const EMAIL_REPUTATION_TASK_QUEUE = 'email-reputation-task-queue'
export const EMAIL_REPUTATION_SCHEDULE_ID = 'email-reputation-evaluation'
export const EMAIL_REPUTATION_WORKFLOW_TYPE = 'emailReputationEvaluation'

/**
 * Hosts the Temporal worker for email reputation evaluation inside the plugin server and
 * idempotently ensures the Temporal Schedule that triggers it. Follows the session-replay
 * rasterizer's Temporal setup (TLS + payload encryption) but registers a TS workflow via
 * workflowsPath rather than activities only.
 */
export class EmailReputationWorkerService {
    private worker?: Worker
    private workerConnection?: NativeConnection
    private clientConnection?: Connection
    private runPromise?: Promise<void>

    constructor(
        private config: PluginsServerConfig,
        private deps: { postgres: PostgresRouter; pubSub: PubSub }
    ) {}

    public async start(): Promise<void> {
        const service = this.buildReputationService()
        const address = `${this.config.TEMPORAL_HOST}:${this.config.TEMPORAL_PORT || '7233'}`
        const namespace = this.config.TEMPORAL_NAMESPACE || 'default'
        const tls = await this.buildTLSConfig()
        const dataConverter = this.buildDataConverter()

        this.workerConnection = await NativeConnection.connect({ address, tls: tls ?? undefined })
        this.worker = await Worker.create({
            connection: this.workerConnection,
            namespace,
            taskQueue: EMAIL_REPUTATION_TASK_QUEUE,
            workflowsPath: require.resolve('./workflow'),
            activities: createActivities(service),
            maxConcurrentActivityTaskExecutions: 2,
            dataConverter,
        })

        this.runPromise = this.worker.run().catch((error) => {
            logger.error('[EmailReputationWorker] worker crashed', { error })
            throw error
        })

        this.clientConnection = await Connection.connect({ address, tls: tls ?? false })
        const client = new Client({ connection: this.clientConnection, namespace, dataConverter })
        await this.ensureSchedule(client)

        logger.info('[EmailReputationWorker] started', { address, taskQueue: EMAIL_REPUTATION_TASK_QUEUE })
    }

    private buildReputationService(): EmailReputationService {
        const chScheme = this.config.CLICKHOUSE_SECURE ? 'https' : 'http'
        const chPort = this.config.CLICKHOUSE_SECURE ? 8443 : 8123
        const clickhouse = createClickHouseClient({
            url: `${chScheme}://${this.config.CLICKHOUSE_HOST}:${chPort}`,
            username: this.config.CLICKHOUSE_USER,
            password: this.config.CLICKHOUSE_PASSWORD || undefined,
            database: this.config.CLICKHOUSE_DATABASE,
        })
        const internalFetch = new InternalFetchService(
            this.config.INTERNAL_API_BASE_URL,
            this.config.INTERNAL_API_SECRET
        )
        return new EmailReputationService(clickhouse, this.deps.postgres, this.deps.pubSub, internalFetch, {
            windowHours: this.config.EMAIL_REPUTATION_WINDOW_HOURS,
            thresholds: {
                minSends: this.config.EMAIL_REPUTATION_MIN_SENDS,
                bounceWarn: this.config.EMAIL_REPUTATION_BOUNCE_WARN_RATE,
                bouncePause: this.config.EMAIL_REPUTATION_BOUNCE_PAUSE_RATE,
                complaintWarn: this.config.EMAIL_REPUTATION_COMPLAINT_WARN_RATE,
                complaintPause: this.config.EMAIL_REPUTATION_COMPLAINT_PAUSE_RATE,
                warnGraceMinutes: this.config.EMAIL_REPUTATION_WARN_GRACE_MINUTES,
            },
        })
    }

    private async ensureSchedule(client: Client): Promise<void> {
        const interval = `${this.config.EMAIL_REPUTATION_EVALUATION_INTERVAL_MINUTES}m`
        try {
            await client.schedule.create({
                scheduleId: EMAIL_REPUTATION_SCHEDULE_ID,
                spec: { intervals: [{ every: interval }] },
                action: {
                    type: 'startWorkflow',
                    workflowType: EMAIL_REPUTATION_WORKFLOW_TYPE,
                    taskQueue: EMAIL_REPUTATION_TASK_QUEUE,
                    args: [],
                },
                policies: { overlap: ScheduleOverlapPolicy.SKIP },
            })
            logger.info('[EmailReputationWorker] created schedule', { interval })
        } catch (error) {
            if (error instanceof ScheduleAlreadyRunning) {
                return
            }
            throw error
        }
    }

    private async buildTLSConfig(): Promise<{
        serverRootCACertificate: Buffer
        clientCertPair: { crt: Buffer; key: Buffer }
    } | null> {
        const { TEMPORAL_CLIENT_ROOT_CA, TEMPORAL_CLIENT_CERT, TEMPORAL_CLIENT_KEY } = this.config
        if (!(TEMPORAL_CLIENT_ROOT_CA && TEMPORAL_CLIENT_CERT && TEMPORAL_CLIENT_KEY)) {
            return null
        }

        let systemCAs = Buffer.alloc(0)
        try {
            systemCAs = Buffer.from(await fs.readFile('/etc/ssl/certs/ca-certificates.crt'))
        } catch {
            // System CA bundle not found — use only the provided root CA
        }

        return {
            serverRootCACertificate: Buffer.concat([systemCAs, Buffer.from(TEMPORAL_CLIENT_ROOT_CA)]),
            clientCertPair: {
                crt: Buffer.from(TEMPORAL_CLIENT_CERT),
                key: Buffer.from(TEMPORAL_CLIENT_KEY),
            },
        }
    }

    private buildDataConverter(): DataConverter | undefined {
        const { TEMPORAL_SECRET_KEY, TEMPORAL_FALLBACK_SECRET_KEYS } = this.config
        if (!TEMPORAL_SECRET_KEY) {
            logger.warn('[EmailReputationWorker] no TEMPORAL_SECRET_KEY configured — payloads will not be encrypted')
            return undefined
        }
        const fallbackKeys = (TEMPORAL_FALLBACK_SECRET_KEYS ?? '')
            .split(',')
            .map((key) => key.trim())
            .filter(Boolean)
        return { payloadCodecs: [new EncryptionCodec(TEMPORAL_SECRET_KEY, fallbackKeys)] }
    }

    public isHealthy(): HealthCheckResult {
        const state = this.worker?.getState()
        if (state !== 'RUNNING') {
            return new HealthCheckResultError(`Email reputation Temporal worker is ${state ?? 'not started'}`, {})
        }
        return new HealthCheckResultOk()
    }

    public async stop(): Promise<void> {
        this.worker?.shutdown()
        // run() resolves once shutdown drains in-flight activities
        await this.runPromise?.catch(() => {})
        await this.clientConnection?.close().catch(() => {})
        await this.workerConnection?.close().catch(() => {})
    }

    public get service(): PluginServerService {
        return {
            id: 'email-reputation-evaluator',
            onShutdown: () => this.stop(),
            healthcheck: () => this.isHealthy(),
        }
    }
}
