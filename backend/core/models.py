from enum import Enum
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

# ─────────────────────────────────────────────
#  枚举：限定 LLM 输出的合法取值，防止乱填
# ─────────────────────────────────────────────

class EventImportance(str, Enum):
    """事件重要性等级"""
    KEY = "key"           # 关键事件，主线必经
    NORMAL = "normal"     # 普通事件
    OPTIONAL = "optional" # 可跳过的支线事件


class PhaseType(str, Enum):
    """三幕结构阶段"""
    SETUP = "setup"                 # 开端：交代背景、引入冲突
    CONFRONTATION = "confrontation" # 冲突：矛盾激化
    RESOLUTION = "resolution"       # 结局：冲突收束


class CharacterImportance(str, Enum):
    """角色重要性等级"""
    PROTAGONIST = "protagonist" # 主角
    KEY = "key"                 # 关键配角
    SUPPORTING = "supporting"   # 普通配角
    MINOR = "minor"             # 路人


class LocationImportance(str, Enum):
    """地点重要性等级"""
    KEY = "key"             # 关键场所，核心剧情发生地
    NORMAL = "normal"       # 普通地点
    BACKGROUND = "background" # 背景地点，仅作氛围


class ItemImportance(str, Enum):
    """物品重要性等级"""
    KEY = "key"
    NORMAL = "normal"
    BACKGROUND = "background"


class LocationType(str, Enum):
    """地点类型：用于构建地图层级结构"""
    REGION = "region"           # 区域（如"江南"）
    SETTLEMENT = "settlement"   # 城镇/村庄
    BUILDING = "building"       # 建筑（如"皇宫"）
    ROOM = "room"               # 房间（如"御书房"）
    WILDERNESS = "wilderness"   # 野外
    PATH = "path"               # 道路/通道


class ItemCategory(str, Enum):
    """物品分类"""
    WEAPON = "weapon"       # 武器
    TOOL = "tool"           # 工具
    CONTAINER = "container" # 容器
    DOCUMENT = "document"   # 文件/书信/奏疏
    KEY_ITEM = "key_item"   # 剧情关键道具
    CONSUMABLE = "consumable" # 消耗品
    OTHER = "other"


class RelationType(str, Enum):
    """角色间关系类型"""
    MASTER = "master"           # 师父
    DISCIPLE = "disciple"       # 徒弟
    ALLY = "ally"               # 盟友
    ENEMY = "enemy"             # 敌人
    RIVAL = "rival"             # 竞争对手
    FRIEND = "friend"           # 朋友
    FAMILY = "family"           # 家人
    ACQUAINTANCE = "acquaintance" # 泛泛之交
    SUBORDINATE = "subordinate" # 下属
    SUPERIOR = "superior"       # 上级
    PARTNER = "partner"         # 伙伴


# ─────────────────────────────────────────────
#  文本分割层：小说原文 → 句子序列
# ─────────────────────────────────────────────

class Sentence(BaseModel):
    """单个句子，由 spaCy 切分后生成"""
    index: int = Field(description="句子编号，从 1 开始")
    text: str = Field(description="句子内容")
    start: int = Field(description="在原文中的起始字符位置")
    end: int = Field(description="在原文中的结束字符位置")


class SentenceData(BaseModel):
    """整部小说的句子列表，预处理第一步的输出"""
    total_sentences: int
    total_characters: int
    sentences: list[Sentence]


# ─────────────────────────────────────────────
#  事件层：小说剧情的基本单元
# ─────────────────────────────────────────────

class EventPhaseDetail(BaseModel):
    """三幕结构中单个阶段的内容"""
    sentence_range: Optional[list[int]] = Field(
        default=None,
        description="该阶段对应的句子范围 [start, end]，可为空",
    )
    description: str = Field(default="", description="阶段内容描述")
    # decision_text 由 DecisionTextExtractor 在后处理阶段回填，初始为空
    decision_text: str = Field(default="", description="决策摘要（由 DecisionTextExtractor 后填）")


class Event(BaseModel):
    """
    一个剧情事件，是 Runtime 游戏引擎推进叙事的最小单位。

    type 决定玩家能否介入：
      - interactive：玩家可输入行动，AI 根据行动生成叙事
      - narrative：纯过场，AI 自动叙述，玩家无法干预
    """
    id: str = Field(description="唯一标识符，使用 snake_case")
    type: Literal["interactive", "narrative"] = Field(description="事件类型")
    goal: str = Field(description="这个事件要达成什么（对叙事推进的意义）")
    sentence_range: list[int] = Field(description="句子编号范围 [start, end]，闭区间")
    importance: EventImportance
    soft_guide_hints: list[str] = Field(
        default_factory=list,
        description="给 Writer 的软引导提示，用于玩家卡住/偏离时",
    )
    # phases 仅 interactive 事件有值；narrative 事件用 narrative 字段
    phases: Optional[dict[str, EventPhaseDetail]] = Field(
        default=None,
        description='三幕结构 {"setup": ..., "confrontation": ..., "resolution": ...}',
    )
    narrative: Optional[str] = Field(
        default=None,
        description="叙事型事件的内容概括",
    )
    decision_text: str = Field(default="", description="事件级决策摘要")
    image: str | None = Field(default=None, description="事件配图在 .wpkg 中的相对路径")


class EventData(BaseModel):
    """所有事件的集合，对应 events.json"""
    events: list[Event]


# ─────────────────────────────────────────────
#  Lorebook 层：世界设定书（角色/地点/物品/知识）
#  Runtime 的 ContextEnrichmentAgent 会查询这里
# ─────────────────────────────────────────────

class CharacterIdentity(BaseModel):
    """角色的身份信息"""
    role: str = Field(description="身份/职位")
    affiliation: Optional[str] = Field(default=None, description="所属势力/门派")


class CharacterPersonality(BaseModel):
    """角色性格，用于 NarrativeGenerationAgent 模拟角色行为"""
    traits: list[str] = Field(description="性格特征列表")
    speaking_style: Optional[str] = Field(default=None, description="说话风格描述")
    motivations: Optional[list[str]] = Field(default=None, description="主要动机/目标")
    fears: Optional[list[str]] = Field(default=None, description="恐惧/弱点")


class CharacterAppearance(BaseModel):
    """角色外貌"""
    physical: Optional[str] = Field(default=None, description="外貌描述")
    distinctive_features: Optional[list[str]] = Field(default=None, description="标志性特征")
    typical_attire: Optional[str] = Field(default=None, description="典型穿着")

    @model_validator(mode='before')
    @classmethod
    def _coerce_attire(cls, data: dict) -> dict:
        # LLM 有时把穿着描述返回成列表，统一合并为字符串
        if isinstance(data, dict):
            v = data.get('typical_attire')
            if isinstance(v, list):
                data['typical_attire'] = '、'.join(v)
        return data


class CharacterRelationship(BaseModel):
    """角色与另一角色之间的关系"""
    target_id: str = Field(description="另一角色ID")
    type: RelationType = Field(description="关系类型")
    description: str = Field(description="关系描述")
    initial_attitude: int = Field(description="初始态度值，-100到100")


class Character(BaseModel):
    """
    角色档案，存入 Lorebook。
    dialogue_examples 是典型台词，让 NarrativeGenerationAgent 模仿该角色的说话风格。
    """
    id: str = Field(description="唯一标识符")
    name: str = Field(description="角色名字")
    aliases: Optional[list[str]] = Field(default_factory=list, description="其他称呼/别名")
    importance: CharacterImportance
    identity: CharacterIdentity
    personality: Optional[CharacterPersonality] = Field(default=None)
    appearance: Optional[CharacterAppearance] = Field(default=None)
    relationships: list[CharacterRelationship] = Field(default_factory=list)
    dialogue_examples: list[str] = Field(default_factory=list, description="典型台词")


class CharacterData(BaseModel):
    """所有角色的集合，对应 characters.json"""
    characters: list[Character]


class LocationDescription(BaseModel):
    """地点的五感描述，用于 NarrativeGenerationAgent 生成沉浸式场景文本"""
    overview: str = Field(description="地点概述")
    atmosphere: Optional[str] = Field(default=None, description="氛围描述")
    visual_details: Optional[list[str]] = Field(default=None, description="视觉细节")
    sounds: Optional[list[str]] = Field(default=None, description="声音描述")
    smells: Optional[list[str]] = Field(default=None, description="气味描述")
    notable_features: Optional[list[str]] = Field(default=None, description="标志性特征")


class LocationConnection(BaseModel):
    """地点间的连接关系，构成世界地图的有向图"""
    location_id: str = Field(description="相连地点ID")
    direction: str = Field(description="方向")
    travel_description: Optional[str] = Field(default=None, description="移动描述")
    accessibility: Optional[str] = Field(default=None, description="通行条件")


class Location(BaseModel):
    """
    地点档案。
    parent_location 构建层级结构，如 御书房 → 皇宫 → 北京城。
    connected_to 构建平面连通图，用于场景切换。
    """
    id: str = Field(description="唯一标识符")
    name: str = Field(description="地点名称")
    aliases: list[str] = Field(default_factory=list, description="别名")
    importance: LocationImportance
    type: LocationType
    parent_location: Optional[str] = Field(default=None, description="父级地点ID")
    description: LocationDescription
    connected_to: list[LocationConnection] = Field(default_factory=list)


class LocationData(BaseModel):
    """所有地点的集合，对应 locations.json"""
    locations: list[Location]


class ItemDescription(BaseModel):
    """物品外观描述"""
    appearance: str = Field(description="外观描述")
    material: Optional[str] = Field(default=None, description="材质")
    size: Optional[str] = Field(default=None, description="大小描述")


class ItemFunction(BaseModel):
    """物品功能"""
    primary_use: str = Field(description="主要用途")
    special_abilities: Optional[list[str]] = Field(default=None, description="特殊能力")
    limitations: Optional[list[str]] = Field(default=None, description="使用限制")

    @field_validator("special_abilities", "limitations", mode="before")
    @classmethod
    def coerce_str_to_list(cls, v: object) -> object:
        # LLM 有时返回单个字符串而非数组，转为单元素列表
        if isinstance(v, str):
            return [v]
        return v


class ItemSignificance(BaseModel):
    """物品在叙事中的意义"""
    narrative_role: Optional[str] = Field(default=None, description="在故事中的作用")
    symbolic_meaning: Optional[str] = Field(default=None, description="象征意义")


class Item(BaseModel):
    """物品档案，存入 Lorebook"""
    id: str = Field(description="唯一标识符")
    name: str = Field(description="物品名称")
    aliases: list[str] = Field(default_factory=list, description="别名")
    importance: ItemImportance
    category: ItemCategory
    description: ItemDescription
    function: Optional[ItemFunction] = Field(default=None)
    significance: Optional[ItemSignificance] = Field(default=None)


class ItemData(BaseModel):
    """所有物品的集合，对应 items.json"""
    items: list[Item]


class Knowledge(BaseModel):
    """
    知识/秘密条目。
    initial_holders 记录最初知道这件事的角色，
    Runtime 中可用于判断某角色是否"应该知道"某信息。
    """
    id: str = Field(description="唯一标识符")
    name: str = Field(description="信息简述")
    initial_holders: list[str] = Field(description="最初知晓此信息的角色 ID 列表")
    description: str = Field(description="信息的具体内容")


class KnowledgeData(BaseModel):
    """所有知识条目的集合，对应 knowledge.json"""
    knowledge: list[Knowledge]


class LorebookData(BaseModel):
    """
    世界设定书，打包四类 Lorebook 数据。
    Runtime 的 ContextEnrichmentAgent 加载此对象查询世界信息。
    """
    characters: list[Character]
    locations: list[Location]
    items: list[Item]
    knowledge: list[Knowledge]


# ─────────────────────────────────────────────
#  实体转换层：追踪事件前后世界状态的变化
#  供 DeviationGuidanceAgent 检测玩家行为是否违反世界逻辑
# ─────────────────────────────────────────────

class Precondition(BaseModel):
    """
    事件发生的前提条件。
    例：事件"海瑞上疏"的前提是 奏疏.持有者 == 海瑞。

    granularity 区分两种精度：
      - named：必须是指定的具体实体（"海瑞"本人）
      - functional：任何能承担该功能的实体都行（"某位官员"）
    """
    name: str = Field(description="实体名")
    type: Literal["character", "item", "information", "location"]
    attribute: Literal["地点", "持有者", "知晓者"]
    from_value: Optional[str] = Field(default=None, description="事件开始前的归属（unnecessary 实体置 null）", alias="from")
    granularity: Literal["named", "functional"]

    model_config = {"populate_by_name": True}


class Effect(BaseModel):
    """
    事件产生的效果：某实体的某属性从 from_value 变为 to。
    例：奏疏.持有者 从 海瑞 → 嘉靖。
    """
    name: str = Field(description="实体名")
    type: Literal["character", "item", "information", "location"]
    attribute: Literal["地点", "持有者", "知晓者"]
    from_value: Optional[str] = Field(default=None, description="变化前的归属（unnecessary 实体置 null）", alias="from")
    to: Optional[str] = Field(default=None, description="变化后的归属（unnecessary 实体置 null）")
    granularity: Literal["named", "functional"]

    model_config = {"populate_by_name": True}


class EventTransition(BaseModel):
    """单个事件的前提条件 + 产生效果，构成状态转换规则"""
    event_id: str
    preconditions: list[Precondition] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)


class TransitionData(BaseModel):
    """所有事件转换规则的集合，对应 transitions.json"""
    transitions: list[EventTransition]


# ─────────────────────────────────────────────
#  必要性分析层：判断实体是否真的必要
# ─────────────────────────────────────────────

class NecessityReasoning(BaseModel):
    """
    对单个实体做反事实推理：
    "如果没有这个实体，事件还能发生吗？"
    """
    entity: str
    type: Literal["character", "item", "information", "location"]
    step_a_counterfactual: str = Field(description="反事实推理")
    necessary: bool
    step_b_substitution: Optional[str] = Field(default=None, description="替代性推理")
    granularity: Optional[Literal["named", "functional"]] = None
    forward_references: list[str] = Field(default_factory=list)


class NecessaryEntity(BaseModel):
    """经过必要性分析后确认必要的实体"""
    name: str
    granularity: Literal["named", "functional"]


class NecessaryEntities(BaseModel):
    """一个事件中所有必要实体，按类型分组"""
    characters: list[NecessaryEntity] = Field(default_factory=list)
    items: list[NecessaryEntity] = Field(default_factory=list)
    information: list[NecessaryEntity] = Field(default_factory=list)
    locations: list[NecessaryEntity] = Field(default_factory=list)


class EventNecessity(BaseModel):
    """单个事件的必要性分析结果"""
    event_id: str
    reasoning: list[NecessityReasoning]
    necessary_entities: NecessaryEntities


class NecessityData(BaseModel):
    """所有事件必要性分析结果的集合"""
    events: list[EventNecessity]


# ─────────────────────────────────────────────
#  校验层：预处理流水线自检，检测实体转换逻辑错误
# ─────────────────────────────────────────────

class TransitionError(BaseModel):
    """单条转换错误记录"""
    type: Literal[
        "ability_leak",              # 角色不应具备某能力却有了
        "continuity_break",          # 前后状态不连续
        "precondition_missing",      # 事件前提条件未列出
        "precondition_redundant",    # 列出了不必要的前提条件
        "annotation_inconsistent",   # 标注前后矛盾
        "granularity_misjudge",      # named/functional 判断错误
        "unnecessary_entity_reference", # 引用了不必要的实体
    ]
    entity: str
    current_granularity: Optional[str] = None
    suggested_granularity: Optional[str] = None
    evidence: str    # 支撑该错误判断的原文证据
    description: str # 错误描述
    suggestion: str  # 修复建议


class EventValidationReport(BaseModel):
    """单个事件的校验报告"""
    event_id: str
    errors: list[TransitionError]


class ValidationReport(BaseModel):
    """所有事件校验报告的集合"""
    reports: list[EventValidationReport]


# ─────────────────────────────────────────────
#  元信息：.wpkg 包的统计摘要
# ─────────────────────────────────────────────

class Metadata(BaseModel):
    """
    世界包元信息，存入 metadata.json。
    供前端 Library 页面展示世界包基本信息。
    """
    title: str = Field(description="作品标题")
    source_file: str = Field(description="源文件路径")
    total_characters: int = Field(description="总字符数")
    total_sentences: int = Field(description="总句子数")
    event_count: int = Field(description="事件数量")
    character_count: int = Field(description="角色数量")
    location_count: int = Field(description="地点数量")
    item_count: int = Field(description="物品数量")
    knowledge_count: int = Field(default=0, description="知识/信息数量")
    transition_count: int = Field(default=0, description="实体转移数量")
    created_at: str = Field(description="创建时间 ISO 格式")
