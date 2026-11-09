-- 014_site_profiles：区域级站点画像（静态/慢变孕灾属性）。
-- 一个区一条；属性与溯源整体存 JSONB——结构由 `aegis.domain.site` 定义，
-- 库侧不再拍平一份列，否则加一个勘测属性就要动迁移。
-- 动态触发量（雨量、位移速率、湖水位）不得写进 attributes：那是遥测的事实，
-- 两处并存必然出现"同一个因子两个值"的口径漂移。
CREATE TABLE IF NOT EXISTS site_profiles (
    region_code  text PRIMARY KEY,
    name         text NOT NULL DEFAULT '',
    hazard_types text[] NOT NULL DEFAULT '{}',
    attributes   jsonb NOT NULL DEFAULT '[]'::jsonb,
    provenance   jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT site_profiles_region_code_shape CHECK (region_code ~ '^[0-9A-Z]{6,24}$'),
    CONSTRAINT site_profiles_attributes_array CHECK (jsonb_typeof(attributes) = 'array'),
    CONSTRAINT site_profiles_provenance_object CHECK (jsonb_typeof(provenance) = 'object')
);
