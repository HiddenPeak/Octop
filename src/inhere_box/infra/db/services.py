"""Database services — RepoBundle and SharedServices DI container."""

from __future__ import annotations

from dataclasses import dataclass

from inhere_box.config import InhereBoxConfig
from inhere_box.infra.crypto.sealed_repos import (
    SealedBackendRepo,
    SealedProviderRepo,
    SealedVoiceProviderRepo,
)
from inhere_box.infra.db.pool import DatabasePool
from inhere_box.infra.db.repos.agents import AgentRepo
from inhere_box.infra.db.repos.audit import AuditRepo
from inhere_box.infra.db.repos.backends import BackendRepo
from inhere_box.infra.db.repos.box_accounts import BoxAccountRepo
from inhere_box.infra.db.repos.bridge_connections import BridgeConnectionRepo
from inhere_box.infra.db.repos.care_push import CarePushRepo
from inhere_box.infra.db.repos.channels import ChannelRepo
from inhere_box.infra.db.repos.connectors import ConnectorRepo
from inhere_box.infra.db.repos.conversation_migrations import ConversationMigrationRepo
from inhere_box.infra.db.repos.cron import CronJobRepo
from inhere_box.infra.db.repos.expert_memory_jobs import ExpertMemoryJobRepo
from inhere_box.infra.db.repos.external_imports import ExternalImportRepo
from inhere_box.infra.db.repos.federated_tasks import FederatedTaskRepo
from inhere_box.infra.db.repos.invites import InviteRepo
from inhere_box.infra.db.repos.knowledge import KnowledgeRepo
from inhere_box.infra.db.repos.model_calls import ModelCallRepo
from inhere_box.infra.db.repos.proactive_care_config import ProactiveCareConfigRepo
from inhere_box.infra.db.repos.project_sync import ProjectSyncRepo
from inhere_box.infra.db.repos.project_task_artifacts import ProjectTaskArtifactRepo
from inhere_box.infra.db.repos.project_task_attachments import ProjectTaskAttachmentRepo
from inhere_box.infra.db.repos.project_task_branches import ProjectTaskBranchRepo
from inhere_box.infra.db.repos.project_task_execution_receipts import (
    ProjectTaskExecutionReceiptRepo,
)
from inhere_box.infra.db.repos.project_task_legacy_sources import LegacyConversationSourceRepo
from inhere_box.infra.db.repos.project_task_reviews import ProjectTaskReviewRepo
from inhere_box.infra.db.repos.project_task_titles import ProjectTaskTitleRepo
from inhere_box.infra.db.repos.project_task_tool_calls import ProjectTaskToolCallRepo
from inhere_box.infra.db.repos.project_task_turns import ProjectTaskTurnRepo
from inhere_box.infra.db.repos.project_tasks import ProjectTaskRepo
from inhere_box.infra.db.repos.projects import ProjectRepo
from inhere_box.infra.db.repos.providers import ProviderRepo
from inhere_box.infra.db.repos.published_experts import PublishedExpertRepo
from inhere_box.infra.db.repos.resident_conversations import ResidentConversationRepo
from inhere_box.infra.db.repos.resident_delegations import (
    ResidentDelegationRepo,
    ResidentTaskReportRepo,
)
from inhere_box.infra.db.repos.resident_followups import ResidentFollowupRepo
from inhere_box.infra.db.repos.resident_history import ResidentHistoryRepo
from inhere_box.infra.db.repos.resident_inbox import ResidentInboxRepo
from inhere_box.infra.db.repos.resident_memory_policy import ResidentMemoryPolicyRepo
from inhere_box.infra.db.repos.resident_notifications import ResidentNotificationRepo
from inhere_box.infra.db.repos.resident_profiles import ResidentProfileRepo
from inhere_box.infra.db.repos.resident_requests import ResidentRequestRepo
from inhere_box.infra.db.repos.secrets import SecretRepo
from inhere_box.infra.db.repos.sessions import SessionRepo
from inhere_box.infra.db.repos.settings import SettingsRepo
from inhere_box.infra.db.repos.skill_packages import SkillPackageRepo
from inhere_box.infra.db.repos.sso import SsoRepo
from inhere_box.infra.db.repos.thread_messages import ThreadMessageRepo
from inhere_box.infra.db.repos.threads import ThreadRepo
from inhere_box.infra.db.repos.trajectory_events import TrajectoryEventRepo
from inhere_box.infra.db.repos.unified_sources import (
    CronOccurrenceRepo,
    UnifiedDeliveryRepo,
    UnifiedSourceInboxRepo,
    UnifiedSourceRouteRepo,
)
from inhere_box.infra.db.repos.usage import UsageRepo
from inhere_box.infra.db.repos.user_policies import UserPolicyRepo
from inhere_box.infra.db.repos.user_task_approval_policies import UserTaskApprovalPolicyRepo
from inhere_box.infra.db.repos.users import UserRepo
from inhere_box.infra.db.repos.voice_providers import VoiceProviderRepo
from inhere_box.infra.utils.paths import PathLayout


@dataclass(frozen=True)
class RepoBundle:
    db: DatabasePool

    box_account_repo: BoxAccountRepo
    conversation_migration_repo: ConversationMigrationRepo
    unified_source_route_repo: UnifiedSourceRouteRepo
    unified_source_inbox_repo: UnifiedSourceInboxRepo
    unified_delivery_repo: UnifiedDeliveryRepo
    cron_occurrence_repo: CronOccurrenceRepo
    federated_task_repo: FederatedTaskRepo

    expert_memory_job_repo: ExpertMemoryJobRepo
    project_sync_repo: ProjectSyncRepo
    project_task_repo: ProjectTaskRepo
    project_task_branch_repo: ProjectTaskBranchRepo
    project_task_legacy_source_repo: LegacyConversationSourceRepo
    user_task_approval_policy_repo: UserTaskApprovalPolicyRepo
    project_task_title_repo: ProjectTaskTitleRepo
    project_task_review_repo: ProjectTaskReviewRepo
    project_task_artifact_repo: ProjectTaskArtifactRepo
    project_task_attachment_repo: ProjectTaskAttachmentRepo
    project_task_execution_receipt_repo: ProjectTaskExecutionReceiptRepo
    project_task_tool_call_repo: ProjectTaskToolCallRepo
    project_task_turn_repo: ProjectTaskTurnRepo
    project_repo: ProjectRepo
    external_import_repo: ExternalImportRepo
    user_repo: UserRepo
    user_policy_repo: UserPolicyRepo
    invite_repo: InviteRepo
    agent_repo: AgentRepo
    resident_conversation_repo: ResidentConversationRepo
    resident_request_repo: ResidentRequestRepo
    resident_history_repo: ResidentHistoryRepo
    resident_delegation_repo: ResidentDelegationRepo
    resident_task_report_repo: ResidentTaskReportRepo
    resident_profile_repo: ResidentProfileRepo
    resident_followup_repo: ResidentFollowupRepo
    resident_notification_repo: ResidentNotificationRepo
    resident_inbox_repo: ResidentInboxRepo
    resident_memory_policy_repo: ResidentMemoryPolicyRepo
    provider_repo: SealedProviderRepo
    channel_repo: ChannelRepo
    cron_repo: CronJobRepo
    session_repo: SessionRepo
    thread_repo: ThreadRepo
    thread_message_repo: ThreadMessageRepo
    trajectory_event_repo: TrajectoryEventRepo
    secret_repo: SecretRepo
    audit_repo: AuditRepo
    usage_repo: UsageRepo
    model_call_repo: ModelCallRepo
    settings_repo: SettingsRepo
    storage_backend_repo: SealedBackendRepo
    bridge_connection_repo: BridgeConnectionRepo
    connector_repo: ConnectorRepo
    skill_package_repo: SkillPackageRepo
    published_expert_repo: PublishedExpertRepo
    knowledge_repo: KnowledgeRepo
    voice_provider_repo: SealedVoiceProviderRepo
    care_push_repo: CarePushRepo
    proactive_care_config_repo: ProactiveCareConfigRepo
    sso_repo: SsoRepo

    @classmethod
    def from_pool(cls, db: DatabasePool) -> RepoBundle:
        secret_repo = SecretRepo(db)
        return cls(
            db=db,
            box_account_repo=BoxAccountRepo(db),
            conversation_migration_repo=ConversationMigrationRepo(db),
            unified_source_route_repo=UnifiedSourceRouteRepo(),
            unified_source_inbox_repo=UnifiedSourceInboxRepo(),
            unified_delivery_repo=UnifiedDeliveryRepo(),
            cron_occurrence_repo=CronOccurrenceRepo(),
            federated_task_repo=FederatedTaskRepo(db),
            expert_memory_job_repo=ExpertMemoryJobRepo(db),
            project_sync_repo=ProjectSyncRepo(),
            project_task_repo=ProjectTaskRepo(db),
            project_task_branch_repo=ProjectTaskBranchRepo(),
            project_task_legacy_source_repo=LegacyConversationSourceRepo(),
            user_task_approval_policy_repo=UserTaskApprovalPolicyRepo(),
            project_task_title_repo=ProjectTaskTitleRepo(db),
            project_task_review_repo=ProjectTaskReviewRepo(db),
            project_task_turn_repo=ProjectTaskTurnRepo(db),
            project_task_tool_call_repo=ProjectTaskToolCallRepo(),
            project_task_artifact_repo=ProjectTaskArtifactRepo(),
            project_task_attachment_repo=ProjectTaskAttachmentRepo(),
            project_task_execution_receipt_repo=ProjectTaskExecutionReceiptRepo(),
            project_repo=ProjectRepo(db),
            external_import_repo=ExternalImportRepo(),
            user_repo=UserRepo(db),
            user_policy_repo=UserPolicyRepo(db),
            invite_repo=InviteRepo(db),
            agent_repo=AgentRepo(db),
            resident_conversation_repo=ResidentConversationRepo(db),
            resident_request_repo=ResidentRequestRepo(db),
            resident_history_repo=ResidentHistoryRepo(db),
            resident_delegation_repo=ResidentDelegationRepo(db),
            resident_task_report_repo=ResidentTaskReportRepo(db),
            resident_profile_repo=ResidentProfileRepo(db),
            resident_followup_repo=ResidentFollowupRepo(db),
            resident_notification_repo=ResidentNotificationRepo(db),
            resident_inbox_repo=ResidentInboxRepo(db),
            resident_memory_policy_repo=ResidentMemoryPolicyRepo(db),
            provider_repo=SealedProviderRepo(ProviderRepo(db), secret_repo),
            channel_repo=ChannelRepo(db),
            cron_repo=CronJobRepo(db),
            session_repo=SessionRepo(db),
            thread_repo=ThreadRepo(db),
            thread_message_repo=ThreadMessageRepo(db),
            trajectory_event_repo=TrajectoryEventRepo(db),
            secret_repo=secret_repo,
            audit_repo=AuditRepo(db),
            usage_repo=UsageRepo(db),
            model_call_repo=ModelCallRepo(db),
            settings_repo=SettingsRepo(db),
            storage_backend_repo=SealedBackendRepo(BackendRepo(db), secret_repo),
            bridge_connection_repo=BridgeConnectionRepo(db),
            connector_repo=ConnectorRepo(db),
            skill_package_repo=SkillPackageRepo(db),
            published_expert_repo=PublishedExpertRepo(db),
            knowledge_repo=KnowledgeRepo(db),
            voice_provider_repo=SealedVoiceProviderRepo(VoiceProviderRepo(db), secret_repo),
            care_push_repo=CarePushRepo(db),
            proactive_care_config_repo=ProactiveCareConfigRepo(db),
            sso_repo=SsoRepo(db),
        )


@dataclass(frozen=True)
class SharedServices:
    paths: PathLayout
    config: InhereBoxConfig
    repos: RepoBundle

    @property
    def box_account_repo(self) -> BoxAccountRepo:
        return self.repos.box_account_repo

    @property
    def db(self) -> DatabasePool:
        return self.repos.db

    @property
    def conversation_migration_repo(self) -> ConversationMigrationRepo:
        return self.repos.conversation_migration_repo

    @property
    def unified_source_route_repo(self) -> UnifiedSourceRouteRepo:
        return self.repos.unified_source_route_repo

    @property
    def unified_source_inbox_repo(self) -> UnifiedSourceInboxRepo:
        return self.repos.unified_source_inbox_repo

    @property
    def unified_delivery_repo(self) -> UnifiedDeliveryRepo:
        return self.repos.unified_delivery_repo

    @property
    def cron_occurrence_repo(self) -> CronOccurrenceRepo:
        return self.repos.cron_occurrence_repo

    @property
    def federated_task_repo(self) -> FederatedTaskRepo:
        return self.repos.federated_task_repo

    @property
    def expert_memory_job_repo(self) -> ExpertMemoryJobRepo:
        return self.repos.expert_memory_job_repo

    @property
    def project_sync_repo(self) -> ProjectSyncRepo:
        return self.repos.project_sync_repo

    @property
    def project_task_repo(self) -> ProjectTaskRepo:
        return self.repos.project_task_repo

    @property
    def project_task_branch_repo(self) -> ProjectTaskBranchRepo:
        return self.repos.project_task_branch_repo

    @property
    def project_task_legacy_source_repo(self) -> LegacyConversationSourceRepo:
        return self.repos.project_task_legacy_source_repo

    @property
    def user_task_approval_policy_repo(self) -> UserTaskApprovalPolicyRepo:
        return self.repos.user_task_approval_policy_repo

    @property
    def project_task_title_repo(self) -> ProjectTaskTitleRepo:
        return self.repos.project_task_title_repo

    @property
    def project_task_review_repo(self) -> ProjectTaskReviewRepo:
        return self.repos.project_task_review_repo

    @property
    def project_task_artifact_repo(self) -> ProjectTaskArtifactRepo:
        return self.repos.project_task_artifact_repo

    @property
    def project_task_attachment_repo(self) -> ProjectTaskAttachmentRepo:
        return self.repos.project_task_attachment_repo

    @property
    def project_task_tool_call_repo(self) -> ProjectTaskToolCallRepo:
        return self.repos.project_task_tool_call_repo

    @property
    def project_task_execution_receipt_repo(self) -> ProjectTaskExecutionReceiptRepo:
        return self.repos.project_task_execution_receipt_repo

    @property
    def project_task_turn_repo(self) -> ProjectTaskTurnRepo:
        return self.repos.project_task_turn_repo

    @property
    def project_repo(self) -> ProjectRepo:
        return self.repos.project_repo

    @property
    def external_import_repo(self) -> ExternalImportRepo:
        return self.repos.external_import_repo

    @property
    def user_repo(self) -> UserRepo:
        return self.repos.user_repo

    @property
    def user_policy_repo(self) -> UserPolicyRepo:
        return self.repos.user_policy_repo

    @property
    def invite_repo(self) -> InviteRepo:
        return self.repos.invite_repo

    @property
    def agent_repo(self) -> AgentRepo:
        return self.repos.agent_repo

    @property
    def resident_conversation_repo(self) -> ResidentConversationRepo:
        return self.repos.resident_conversation_repo

    @property
    def resident_request_repo(self) -> ResidentRequestRepo:
        return self.repos.resident_request_repo

    @property
    def resident_history_repo(self) -> ResidentHistoryRepo:
        return self.repos.resident_history_repo

    @property
    def resident_delegation_repo(self) -> ResidentDelegationRepo:
        return self.repos.resident_delegation_repo

    @property
    def resident_task_report_repo(self) -> ResidentTaskReportRepo:
        return self.repos.resident_task_report_repo

    @property
    def resident_profile_repo(self) -> ResidentProfileRepo:
        return self.repos.resident_profile_repo

    @property
    def resident_followup_repo(self) -> ResidentFollowupRepo:
        return self.repos.resident_followup_repo

    @property
    def resident_notification_repo(self) -> ResidentNotificationRepo:
        return self.repos.resident_notification_repo

    @property
    def resident_inbox_repo(self) -> ResidentInboxRepo:
        return self.repos.resident_inbox_repo

    @property
    def resident_memory_policy_repo(self) -> ResidentMemoryPolicyRepo:
        return self.repos.resident_memory_policy_repo

    @property
    def provider_repo(self) -> SealedProviderRepo:
        return self.repos.provider_repo

    @property
    def channel_repo(self) -> ChannelRepo:
        return self.repos.channel_repo

    @property
    def cron_repo(self) -> CronJobRepo:
        return self.repos.cron_repo

    @property
    def session_repo(self) -> SessionRepo:
        return self.repos.session_repo

    @property
    def thread_repo(self) -> ThreadRepo:
        return self.repos.thread_repo

    @property
    def thread_message_repo(self) -> ThreadMessageRepo:
        return self.repos.thread_message_repo

    @property
    def trajectory_event_repo(self) -> TrajectoryEventRepo:
        return self.repos.trajectory_event_repo

    @property
    def secret_repo(self) -> SecretRepo:
        return self.repos.secret_repo

    @property
    def audit_repo(self) -> AuditRepo:
        return self.repos.audit_repo

    @property
    def usage_repo(self) -> UsageRepo:
        return self.repos.usage_repo

    @property
    def model_call_repo(self) -> ModelCallRepo:
        return self.repos.model_call_repo

    @property
    def settings_repo(self) -> SettingsRepo:
        return self.repos.settings_repo

    @property
    def storage_backend_repo(self) -> SealedBackendRepo:
        return self.repos.storage_backend_repo

    @property
    def bridge_connection_repo(self) -> BridgeConnectionRepo:
        return self.repos.bridge_connection_repo

    @property
    def connector_repo(self) -> ConnectorRepo:
        return self.repos.connector_repo

    @property
    def skill_package_repo(self) -> SkillPackageRepo:
        return self.repos.skill_package_repo

    @property
    def published_expert_repo(self) -> PublishedExpertRepo:
        return self.repos.published_expert_repo

    @property
    def knowledge_repo(self) -> KnowledgeRepo:
        return self.repos.knowledge_repo

    @property
    def voice_provider_repo(self) -> SealedVoiceProviderRepo:
        return self.repos.voice_provider_repo

    @property
    def care_push_repo(self) -> CarePushRepo:
        return self.repos.care_push_repo

    @property
    def proactive_care_config_repo(self) -> ProactiveCareConfigRepo:
        return self.repos.proactive_care_config_repo

    @property
    def sso_repo(self) -> SsoRepo:
        return self.repos.sso_repo


def build_shared_services(
    *, db: DatabasePool, paths: PathLayout, config: InhereBoxConfig
) -> SharedServices:
    return SharedServices(
        paths=paths,
        config=config,
        repos=RepoBundle.from_pool(db),
    )
