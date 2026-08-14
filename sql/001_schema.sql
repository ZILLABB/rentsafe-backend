-- RentSafe Lagos — base schema (Section XIII).
-- PostgreSQL 16 + PostGIS. Apply with: psql "$DATABASE_URL" -f sql/001_schema.sql
-- Money is stored in kobo (BIGINT) throughout to avoid floating-point drift.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fuzzy text search on addresses / agent names

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lgas (
  code        VARCHAR(5)  PRIMARY KEY,           -- 'ETI'
  name        VARCHAR(100) NOT NULL,             -- 'Eti-Osa'
  flood_risk  VARCHAR(20),                       -- VeryHigh/High/Moderate/Low
  state       VARCHAR(50) DEFAULT 'Lagos'
);

CREATE TABLE IF NOT EXISTS neighbourhoods (
  code             VARCHAR(10) PRIMARY KEY,       -- 'LEK'
  name             VARCHAR(100) NOT NULL,         -- 'Lekki Phase 1'
  lga_code         VARCHAR(5) REFERENCES lgas(code),
  centroid         GEOGRAPHY(POINT, 4326),
  avg_rent_1bed    BIGINT,
  avg_rent_2bed    BIGINT,
  avg_rent_3bed    BIGINT,
  avg_rating       DECIMAL(3,2),
  total_properties INT DEFAULT 0,
  total_reviews    INT DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Users
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
  id                   SERIAL PRIMARY KEY,
  phone_hash           VARCHAR(64) UNIQUE NOT NULL,   -- SHA-256, never plaintext
  phone_last4          VARCHAR(4),
  nin_verified         BOOLEAN DEFAULT FALSE,
  display_name         VARCHAR(60),
  is_anonymous_default BOOLEAN DEFAULT FALSE,
  role                 VARCHAR(20) DEFAULT 'tenant',  -- tenant/agent/landlord/admin
  review_count         INT DEFAULT 0,
  trust_score          DECIMAL(3,2) DEFAULT 0.50,
  is_active            BOOLEAN DEFAULT TRUE,
  created_at           TIMESTAMP DEFAULT NOW(),
  last_active_at       TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Agents & landlords (referenced by reviews, so created before them)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents (
  id                          SERIAL PRIMARY KEY,
  name                        VARCHAR(100) NOT NULL,
  name_normalised             VARCHAR(100),
  phone_hash                  VARCHAR(64),
  email_hash                  VARCHAR(64),
  lasrera_number              VARCHAR(30),
  lasrera_verified            BOOLEAN DEFAULT FALSE,
  company_name                VARCHAR(200),
  operating_areas             TEXT[],
  bio                         TEXT,
  profile_photo_url           VARCHAR(300),
  avg_rating_transparency     DECIMAL(3,2),
  avg_rating_honesty          DECIMAL(3,2),
  avg_rating_fee_fairness     DECIMAL(3,2),
  avg_rating_responsiveness   DECIMAL(3,2),
  avg_rating_professionalism  DECIMAL(3,2),
  avg_rating_overall          DECIMAL(3,2),
  avg_fee_pct                 DECIMAL(5,2),
  total_reviews               INT DEFAULT 0,
  total_properties            INT DEFAULT 0,
  flagged                     BOOLEAN DEFAULT FALSE,
  flag_reason                 TEXT,
  flag_review_count           INT DEFAULT 0,
  profile_claimed             BOOLEAN DEFAULT FALSE,
  claimed_by_user             INT REFERENCES users(id),
  created_at                  TIMESTAMP DEFAULT NOW(),
  updated_at                  TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_agents_name_trgm ON agents USING gin(name_normalised gin_trgm_ops);

CREATE TABLE IF NOT EXISTS landlords (
  id                       SERIAL PRIMARY KEY,
  display_label            VARCHAR(100),          -- "Landlord of ETI-LEK-7F3A2B-0041"
  nin_verified             BOOLEAN DEFAULT FALSE,
  avg_rating_maintenance   DECIMAL(3,2),
  avg_rating_deposit       DECIMAL(3,2),
  avg_rating_rent_increase DECIMAL(3,2),
  avg_rating_compliance    DECIMAL(3,2),
  avg_rating_communication DECIMAL(3,2),
  avg_rating_overall       DECIMAL(3,2),
  total_reviews            INT DEFAULT 0,
  profile_claimed          BOOLEAN DEFAULT FALSE,
  claimed_by_user          INT REFERENCES users(id),
  created_at               TIMESTAMP DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Properties (Canonical Property Records) — the heart of the identity system
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS properties (
  id                  SERIAL PRIMARY KEY,
  property_id         VARCHAR(25) UNIQUE NOT NULL,    -- ETI-LEK-7F3A2B-0041
  geohash_7           VARCHAR(7)  NOT NULL,
  geohash_8           VARCHAR(8)  NOT NULL,
  lat                 DECIMAL(10,8) NOT NULL,
  lng                 DECIMAL(11,8) NOT NULL,
  location            GEOGRAPHY(POINT, 4326) NOT NULL,
  address_formal      TEXT,
  address_local       TEXT,
  address_normalised  TEXT,
  w3w_address         VARCHAR(60),
  lga_code            VARCHAR(5)  REFERENCES lgas(code),
  neighbourhood_code  VARCHAR(10) REFERENCES neighbourhoods(code),
  property_type       VARCHAR(30),
  bedrooms            SMALLINT,
  flood_zone          VARCHAR(20),
  elevation_m         DECIMAL(6,2),
  drainage_dist_m     DECIMAL(8,2),
  photo_hash          VARCHAR(64),
  street_view_url     TEXT,
  avg_rating          DECIMAL(3,2),
  total_reviews       INT DEFAULT 0,
  verified_reviews    INT DEFAULT 0,
  latest_rent_kobo    BIGINT,
  rent_velocity_pct   DECIMAL(5,2),
  liquidity_score     DECIMAL(3,1),
  traffic_score       VARCHAR(10),
  status              VARCHAR(20) DEFAULT 'active',
  merged_into         INT REFERENCES properties(id),
  created_at          TIMESTAMP DEFAULT NOW(),
  updated_at          TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_properties_location      ON properties USING GIST(location);
CREATE INDEX IF NOT EXISTS idx_properties_geohash8      ON properties(geohash_8);
CREATE INDEX IF NOT EXISTS idx_properties_geohash7      ON properties(geohash_7);
CREATE INDEX IF NOT EXISTS idx_properties_lga           ON properties(lga_code);
CREATE INDEX IF NOT EXISTS idx_properties_neighbourhood ON properties(neighbourhood_code);
CREATE INDEX IF NOT EXISTS idx_properties_property_id   ON properties(property_id);

-- ---------------------------------------------------------------------------
-- Reviews
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reviews (
  id                    SERIAL PRIMARY KEY,
  property_id           INT REFERENCES properties(id) NOT NULL,
  user_id               INT REFERENCES users(id) NOT NULL,
  agent_id              INT REFERENCES agents(id),
  tenancy_start         DATE NOT NULL,
  tenancy_end           DATE,
  still_living          BOOLEAN DEFAULT FALSE,
  rent_amount_kobo      BIGINT,
  rent_period           VARCHAR(20),
  agent_fee_kobo        BIGINT,
  caution_fee_kobo      BIGINT,
  agreement_fee_kobo    BIGINT,
  departure_reason      VARCHAR(50),
  rating_landlord       SMALLINT CHECK (rating_landlord      BETWEEN 1 AND 5),
  rating_agent          SMALLINT CHECK (rating_agent         BETWEEN 1 AND 5),
  rating_property       SMALLINT CHECK (rating_property      BETWEEN 1 AND 5),
  rating_water          SMALLINT CHECK (rating_water         BETWEEN 1 AND 5),
  rating_power          SMALLINT CHECK (rating_power         BETWEEN 1 AND 5),
  rating_security       SMALLINT CHECK (rating_security      BETWEEN 1 AND 5),
  rating_noise          SMALLINT CHECK (rating_noise         BETWEEN 1 AND 5),
  rating_flooding       SMALLINT CHECK (rating_flooding      BETWEEN 1 AND 5),
  rating_neighbourhood  SMALLINT CHECK (rating_neighbourhood BETWEEN 1 AND 5),
  rating_value          SMALLINT CHECK (rating_value         BETWEEN 1 AND 5),
  flood_experienced     VARCHAR(20),
  flood_severity        VARCHAR(20),
  flood_months          SMALLINT[],
  power_hours_avg       SMALLINT,
  water_sources         TEXT[],
  water_quality         VARCHAR(20),
  waste_collection      VARCHAR(20),
  generator_noise       VARCHAR(20),
  internet_quality      TEXT[],
  pest_issues           TEXT[],
  commute_destination   TEXT,
  commute_time_minutes  SMALLINT,
  commute_mode          VARCHAR(30),
  text_positives        TEXT,
  text_warnings         TEXT,
  text_negotiation_tips TEXT,
  verification_tier     SMALLINT DEFAULT 1,
  verified_tenant       BOOLEAN DEFAULT FALSE,
  verification_doc_type VARCHAR(30),
  is_anonymous          BOOLEAN DEFAULT FALSE,
  moderation_status     VARCHAR(20) DEFAULT 'pending',
  moderated_by          INT REFERENCES users(id),
  moderated_at          TIMESTAMP,
  flag_count            INT DEFAULT 0,
  created_at            TIMESTAMP DEFAULT NOW(),
  updated_at           TIMESTAMP DEFAULT NOW(),
  UNIQUE (property_id, user_id, tenancy_start)
);
CREATE INDEX IF NOT EXISTS idx_reviews_property   ON reviews(property_id);
CREATE INDEX IF NOT EXISTS idx_reviews_agent      ON reviews(agent_id);
CREATE INDEX IF NOT EXISTS idx_reviews_moderation ON reviews(moderation_status);
CREATE INDEX IF NOT EXISTS idx_reviews_created    ON reviews(created_at DESC);

-- ---------------------------------------------------------------------------
-- Rent history (denormalised time-series)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rent_history (
  id               SERIAL PRIMARY KEY,
  property_id      INT REFERENCES properties(id) NOT NULL,
  review_id        INT REFERENCES reviews(id) NOT NULL,
  period_year      SMALLINT NOT NULL,
  period_quarter   SMALLINT,
  amount_kobo      BIGINT NOT NULL,
  payment_type     VARCHAR(20),
  agent_fee_kobo   BIGINT,
  caution_fee_kobo BIGINT,
  total_cost_kobo  BIGINT,
  recorded_at      TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rent_history_property ON rent_history(property_id, period_year);

-- ---------------------------------------------------------------------------
-- Evidence
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence (
  id                       SERIAL PRIMARY KEY,
  review_id                INT REFERENCES reviews(id) NOT NULL,
  file_type                VARCHAR(20) NOT NULL,
  evidence_type            VARCHAR(40) NOT NULL,
  original_cloudinary_id   VARCHAR(200),
  processed_cloudinary_id  VARCHAR(200),
  public_thumbnail_url     VARCHAR(300),
  file_size_bytes          BIGINT,
  duration_seconds         INT,
  phash                    VARCHAR(64),
  exif_stripped            BOOLEAN DEFAULT FALSE,
  moderation_status        VARCHAR(20) DEFAULT 'pending',
  moderator_notes          TEXT,
  moderated_by             INT REFERENCES users(id),
  moderated_at             TIMESTAMP,
  uploaded_at              TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_evidence_review     ON evidence(review_id);
CREATE INDEX IF NOT EXISTS idx_evidence_moderation ON evidence(moderation_status);

-- ---------------------------------------------------------------------------
-- Landlord <-> property, responses, audit, alerts, subscriptions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS property_landlords (
  property_id INT REFERENCES properties(id),
  landlord_id INT REFERENCES landlords(id),
  active      BOOLEAN DEFAULT TRUE,
  start_date  DATE,
  end_date    DATE,
  PRIMARY KEY (property_id, landlord_id)
);

CREATE TABLE IF NOT EXISTS responses (
  id                SERIAL PRIMARY KEY,
  review_id         INT REFERENCES reviews(id) NOT NULL,
  responder_type    VARCHAR(20) NOT NULL,         -- agent/landlord
  responder_id      INT NOT NULL,
  response_text     TEXT NOT NULL,
  moderation_status VARCHAR(20) DEFAULT 'pending',
  created_at        TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS merge_log (
  id              SERIAL PRIMARY KEY,
  source_property INT NOT NULL,
  target_property INT REFERENCES properties(id) NOT NULL,
  merged_by       INT REFERENCES users(id),
  merged_at       TIMESTAMP DEFAULT NOW(),
  can_undo_until  TIMESTAMP,
  undone          BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS area_watches (
  id                   SERIAL PRIMARY KEY,
  user_id              INT REFERENCES users(id),
  neighbourhood_code   VARCHAR(10) REFERENCES neighbourhoods(code),
  lga_code             VARCHAR(5)  REFERENCES lgas(code),
  notify_new_reviews   BOOLEAN DEFAULT TRUE,
  notify_flood_reports BOOLEAN DEFAULT TRUE,
  notify_agent_flags   BOOLEAN DEFAULT FALSE,
  created_at           TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id                SERIAL PRIMARY KEY,
  user_id           INT REFERENCES users(id),
  plan_type         VARCHAR(30),
  status            VARCHAR(20) DEFAULT 'active',
  started_at        TIMESTAMP,
  expires_at        TIMESTAMP,
  payment_reference VARCHAR(100),
  amount_kobo       BIGINT
);
