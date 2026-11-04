"""Canonical migration-owned registration security definitions (PostgreSQL 16)."""
def guard_functions(a,m):
    common = "RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $guard$"
    deny = "RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_registration_authority_required',MESSAGE='REGISTRATION_AUTHORITY_REQUIRED';"
    repair = f"(session_user='{m}' AND EXISTS(SELECT 1 FROM kalillac.account_registration_activation WHERE singleton_id=1 AND state='unactivated'))"
    return {
        "registration_control_lock": """(exclusive boolean) RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $guard$
DECLARE s text;
BEGIN
 IF exclusive IS NULL THEN RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_registration_authority_required',MESSAGE='REGISTRATION_STATE_UNREADABLE'; END IF;
 IF exclusive THEN SELECT state INTO s FROM kalillac.account_registration_activation WHERE singleton_id=1 FOR UPDATE;
 ELSE SELECT state INTO s FROM kalillac.account_registration_activation WHERE singleton_id=1 FOR SHARE; END IF;
 IF s IS NULL OR s NOT IN ('unactivated','active_zero_legacy') THEN RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_registration_authority_required',MESSAGE='REGISTRATION_STATE_UNREADABLE'; END IF;
END $guard$""",
        "registration_intent_lifecycle_guard": f"() {common}\nBEGIN\n IF session_user <> '{a}' AND NOT {repair} THEN {deny} END IF;\n" + """
 IF TG_OP='DELETE' THEN
  IF OLD.expires_at > clock_timestamp() THEN RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_registration_authority_required',MESSAGE='REGISTRATION_NOT_EXPIRED'; END IF;
  RETURN OLD;
 END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.state <> 'pending' OR NEW.consumed_xid IS NOT NULL OR NEW.provenance_claimed THEN RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_registration_authority_required',MESSAGE='REGISTRATION_INVALID_TRANSITION'; END IF;
  RETURN NEW;
 END IF;
 IF ROW(NEW.id,NEW.address_id,NEW.generation,NEW.purpose,NEW.format_version,NEW.address_lookup_key_version,NEW.address_lookup_digest,NEW.token_digest,NEW.created_at,NEW.expires_at)
 IS DISTINCT FROM ROW(OLD.id,OLD.address_id,OLD.generation,OLD.purpose,OLD.format_version,OLD.address_lookup_key_version,OLD.address_lookup_digest,OLD.token_digest,OLD.created_at,OLD.expires_at)
 THEN RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_registration_authority_required',MESSAGE='REGISTRATION_IMMUTABLE'; END IF;
 IF OLD.state='pending' AND NEW.state IN ('consumed','superseded','expired') THEN
  IF NEW.provenance_claimed OR NEW.consumed_xid IS NOT NULL THEN RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_registration_authority_required',MESSAGE='REGISTRATION_INVALID_TRANSITION'; END IF;
  IF NEW.state='consumed' THEN
   IF NOT OLD.is_current OR OLD.expires_at <= clock_timestamp() THEN RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_registration_authority_required',MESSAGE='REGISTRATION_NOT_CURRENT'; END IF;
   NEW.consumed_xid := pg_current_xact_id();
  END IF;
  RETURN NEW;
 END IF;
 IF OLD.state='consumed' AND NOT OLD.provenance_claimed AND NEW.provenance_claimed
 AND pg_trigger_depth() >= 2
 AND ROW(NEW.id,NEW.address_id,NEW.generation,NEW.purpose,NEW.format_version,NEW.address_lookup_key_version,NEW.address_lookup_digest,NEW.token_digest,NEW.email_ciphertext,NEW.email_nonce,NEW.email_key_version,NEW.is_current,NEW.state,NEW.created_at,NEW.expires_at,NEW.consumed_at,NEW.superseded_at,NEW.expired_at,NEW.consumed_xid)
 IS NOT DISTINCT FROM ROW(OLD.id,OLD.address_id,OLD.generation,OLD.purpose,OLD.format_version,OLD.address_lookup_key_version,OLD.address_lookup_digest,OLD.token_digest,OLD.email_ciphertext,OLD.email_nonce,OLD.email_key_version,OLD.is_current,OLD.state,OLD.created_at,OLD.expires_at,OLD.consumed_at,OLD.superseded_at,OLD.expired_at,OLD.consumed_xid)
 THEN RETURN NEW; END IF;
 RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_registration_authority_required',MESSAGE='REGISTRATION_INVALID_TRANSITION';
END $guard$""",
        "registration_user_insert_guard": f"() {common}\nDECLARE s text; i kalillac.account_registration_intents%ROWTYPE;\nBEGIN\n SELECT state INTO s FROM kalillac.account_registration_activation WHERE singleton_id=1 FOR SHARE;\n IF s IS NULL THEN {deny} END IF;\n IF NEW.quarantined_at IS NOT NULL OR NEW.sessions_valid_after IS NOT NULL THEN {deny} END IF;\n IF s='unactivated' AND session_user <> '{a}' THEN\n  IF NEW.registration_intent_id IS NOT NULL THEN {deny} END IF;\n  RETURN NEW;\n END IF;\n IF s NOT IN ('unactivated','active_zero_legacy') OR session_user <> '{a}' THEN {deny} END IF;\n" + """
 SELECT * INTO i FROM kalillac.account_registration_intents WHERE id=NEW.registration_intent_id FOR UPDATE;
 IF NOT FOUND OR i.state <> 'consumed' OR i.purpose <> 'finish_registration' OR i.format_version <> 1
 OR i.is_current OR i.consumed_xid IS DISTINCT FROM pg_current_xact_id() OR i.provenance_claimed
 OR i.expires_at <= clock_timestamp() OR i.consumed_at < i.created_at
 OR i.email_ciphertext IS NOT NULL OR i.email_nonce IS NOT NULL OR i.email_key_version IS NOT NULL
 OR NEW.email_verified_at IS NULL OR NOT NEW.is_active
 THEN RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_users_registration_provenance',MESSAGE='REGISTRATION_PROVENANCE_REQUIRED'; END IF;
 UPDATE kalillac.account_registration_intents SET provenance_claimed=true WHERE id=i.id;
 RETURN NEW;
END $guard$""",
        "registration_user_identity_guard": f"() {common}\nDECLARE s text;\nBEGIN\n" + f"""
 IF ROW(NEW.id,NEW.email,NEW.created_at,NEW.registration_intent_id,NEW.quarantined_at,NEW.sessions_valid_after) IS DISTINCT FROM ROW(OLD.id,OLD.email,OLD.created_at,OLD.registration_intent_id,OLD.quarantined_at,OLD.sessions_valid_after) THEN
  IF NOT (session_user='{a}' AND pg_trigger_depth() >= 2 AND OLD.registration_intent_id IS NOT NULL AND NEW.registration_intent_id IS NULL
   AND ROW(NEW.id,NEW.email,NEW.created_at,NEW.quarantined_at,NEW.sessions_valid_after) IS NOT DISTINCT FROM ROW(OLD.id,OLD.email,OLD.created_at,OLD.quarantined_at,OLD.sessions_valid_after)
   AND NOT EXISTS(SELECT 1 FROM kalillac.account_registration_intents WHERE id=OLD.registration_intent_id)) THEN
   RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_users_registration_identity',MESSAGE='REGISTRATION_IDENTITY_IMMUTABLE';
  END IF;
 END IF;
 IF NEW.email_verified_at IS DISTINCT FROM OLD.email_verified_at THEN
  SELECT state INTO s FROM kalillac.account_registration_activation WHERE singleton_id=1 FOR SHARE;
  IF s IS DISTINCT FROM 'unactivated' THEN {deny} END IF;
 END IF;
 RETURN NEW;
END $guard$"""
    }


# V1-02C (0014) identity guard v2: guard_functions above stays 0011's frozen
# text, and v2 adds exactly one allowed transition, the session epoch moving
# strictly forward with every other protected identity value unchanged. A
# definer trigger cannot see its caller, so column privilege bounds who may
# write the epoch.
EPOCH_ADVANCE=("ROW(NEW.id,NEW.email,NEW.created_at,NEW.registration_intent_id,NEW.quarantined_at) IS NOT DISTINCT FROM ROW(OLD.id,OLD.email,OLD.created_at,OLD.registration_intent_id,OLD.quarantined_at)"
    " AND NEW.sessions_valid_after IS NOT NULL AND (OLD.sessions_valid_after IS NULL OR NEW.sessions_valid_after > OLD.sessions_valid_after)")
_IDENTITY_DENY=") THEN\n   RAISE EXCEPTION USING ERRCODE='23514',CONSTRAINT='ck_users_registration_identity'"
def identity_guard_v2(a):
    v1=guard_functions(a,"unused")["registration_user_identity_guard"]
    if v1.count(_IDENTITY_DENY)!=1:raise RuntimeError("REGISTRATION_IDENTITY_GUARD_UNRECOGNIZED")
    return v1.replace(_IDENTITY_DENY,")\n   AND NOT ("+EPOCH_ADVANCE+_IDENTITY_DENY)


# Exact PostgreSQL 16 catalog forms; changes require explicit migration review.
CANONICAL_CATALOG = {'constraints': [['account_registration_activation', 'ck_account_registration_activation_activation_shape', "CHECK (((((state)::text = 'unactivated'::text) AND (activation_id IS NULL) AND (cutover_id IS NULL) AND (deployment_id IS NULL) AND (evidence_sha256 IS NULL) AND (inventory_at IS NULL) AND (activated_at IS NULL) AND (legacy_unverified_count IS NULL)) OR (((state)::text = 'active_zero_legacy'::text) AND (activation_id IS NOT NULL) AND (cutover_id IS NOT NULL) AND (deployment_id IS NOT NULL) AND (evidence_sha256 IS NOT NULL) AND (length((evidence_sha256)::text) = 64) AND (length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace((evidence_sha256)::text, '0'::text, ''::text), '1'::text, ''::text), '2'::text, ''::text), '3'::text, ''::text), '4'::text, ''::text), '5'::text, ''::text), '6'::text, ''::text), '7'::text, ''::text), '8'::text, ''::text), '9'::text, ''::text), 'a'::text, ''::text), 'b'::text, ''::text), 'c'::text, ''::text), 'd'::text, ''::text), 'e'::text, ''::text), 'f'::text, ''::text)) = 0) AND (inventory_at IS NOT NULL) AND (activated_at IS NOT NULL) AND (activated_at >= inventory_at) AND (legacy_unverified_count = 0))))", True, False, False, False], ['account_registration_activation', 'ck_account_registration_activation_activation_singleton', 'CHECK (((singleton_id = 1) AND (policy_version = 1)))', True, False, False, False], ['account_registration_activation', 'pk_account_registration_activation', 'PRIMARY KEY (singleton_id)', True, False, False, True], ['account_registration_activation', 'uq_account_registration_activation_activation_id', 'UNIQUE (activation_id)', True, False, False, True], ['account_registration_activation', 'uq_account_registration_activation_database_instance_id', 'UNIQUE (database_instance_id)', True, False, False, True], ['account_registration_intents', 'ck_account_registration_intents_ciphertext_length', 'CHECK (((email_ciphertext IS NULL) OR ((length(email_ciphertext) >= 17) AND (length(email_ciphertext) <= 1296))))', True, False, False, False], ['account_registration_intents', 'ck_account_registration_intents_digest_length', "CHECK (((length((token_digest)::text) = 64) AND (length((address_lookup_digest)::text) = 64) AND (length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace((token_digest)::text, '0'::text, ''::text), '1'::text, ''::text), '2'::text, ''::text), '3'::text, ''::text), '4'::text, ''::text), '5'::text, ''::text), '6'::text, ''::text), '7'::text, ''::text), '8'::text, ''::text), '9'::text, ''::text), 'a'::text, ''::text), 'b'::text, ''::text), 'c'::text, ''::text), 'd'::text, ''::text), 'e'::text, ''::text), 'f'::text, ''::text)) = 0) AND (length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace((address_lookup_digest)::text, '0'::text, ''::text), '1'::text, ''::text), '2'::text, ''::text), '3'::text, ''::text), '4'::text, ''::text), '5'::text, ''::text), '6'::text, ''::text), '7'::text, ''::text), '8'::text, ''::text), '9'::text, ''::text), 'a'::text, ''::text), 'b'::text, ''::text), 'c'::text, ''::text), 'd'::text, ''::text), 'e'::text, ''::text), 'f'::text, ''::text)) = 0)))", True, False, False, False], ['account_registration_intents', 'ck_account_registration_intents_expiry', 'CHECK ((expires_at > created_at))', True, False, False, False], ['account_registration_intents', 'ck_account_registration_intents_generation', 'CHECK ((generation > 0))', True, False, False, False], ['account_registration_intents', 'ck_account_registration_intents_key_versions', "CHECK (((substr((address_lookup_key_version)::text, 1, 12) = 'reg-lookup-v'::text) AND ((length((address_lookup_key_version)::text) >= 13) AND (length((address_lookup_key_version)::text) <= 21)) AND ((email_key_version IS NULL) OR ((substr((email_key_version)::text, 1, 9) = 'reg-enc-v'::text) AND ((length((email_key_version)::text) >= 10) AND (length((email_key_version)::text) <= 18))))))", True, False, False, False], ['account_registration_intents', 'ck_account_registration_intents_lifecycle', "CHECK (((((state)::text = 'pending'::text) AND is_current AND (email_ciphertext IS NOT NULL) AND (email_nonce IS NOT NULL) AND (email_key_version IS NOT NULL) AND (consumed_at IS NULL) AND (superseded_at IS NULL) AND (expired_at IS NULL)) OR (((state)::text = 'consumed'::text) AND (NOT is_current) AND (email_ciphertext IS NULL) AND (email_nonce IS NULL) AND (email_key_version IS NULL) AND (consumed_at IS NOT NULL) AND (consumed_at >= created_at) AND (superseded_at IS NULL) AND (expired_at IS NULL)) OR (((state)::text = 'superseded'::text) AND (NOT is_current) AND (email_ciphertext IS NULL) AND (email_nonce IS NULL) AND (email_key_version IS NULL) AND (consumed_at IS NULL) AND (superseded_at IS NOT NULL) AND (superseded_at >= created_at) AND (expired_at IS NULL)) OR (((state)::text = 'expired'::text) AND (NOT is_current) AND (email_ciphertext IS NULL) AND (email_nonce IS NULL) AND (email_key_version IS NULL) AND (consumed_at IS NULL) AND (superseded_at IS NULL) AND (expired_at IS NOT NULL) AND (expired_at >= created_at))))", True, False, False, False], ['account_registration_intents', 'ck_account_registration_intents_nonce_length', 'CHECK (((email_nonce IS NULL) OR (length(email_nonce) = 12)))', True, False, False, False], ['account_registration_intents', 'ck_account_registration_intents_purpose_format', "CHECK ((((purpose)::text = 'finish_registration'::text) AND (format_version = 1)))", True, False, False, False], ['account_registration_intents', 'ck_intents_provenance_state', "CHECK (((((state)::text = 'consumed'::text) AND (consumed_xid IS NOT NULL)) OR (((state)::text <> 'consumed'::text) AND (consumed_xid IS NULL) AND (NOT provenance_claimed))))", True, False, False, False], ['account_registration_intents', 'pk_account_registration_intents', 'PRIMARY KEY (id)', True, False, False, True], ['account_registration_intents', 'uq_account_registration_intents_address_id', 'UNIQUE (address_id, generation)', True, False, False, True], ['account_registration_intents', 'uq_account_registration_intents_token_digest', 'UNIQUE (token_digest)', True, False, False, True], ['users', 'fk_users_registration_intent', 'FOREIGN KEY (registration_intent_id) REFERENCES kalillac.account_registration_intents(id) ON DELETE SET NULL', True, False, False, True]], 'indexes': [['account_registration_activation', 'pk_account_registration_activation', 'CREATE UNIQUE INDEX pk_account_registration_activation ON kalillac.account_registration_activation USING btree (singleton_id)', True, True, True, True], ['account_registration_activation', 'uq_account_registration_activation_activation_id', 'CREATE UNIQUE INDEX uq_account_registration_activation_activation_id ON kalillac.account_registration_activation USING btree (activation_id)', True, True, True, True], ['account_registration_activation', 'uq_account_registration_activation_database_instance_id', 'CREATE UNIQUE INDEX uq_account_registration_activation_database_instance_id ON kalillac.account_registration_activation USING btree (database_instance_id)', True, True, True, True], ['account_registration_intents', 'ix_registration_cleanup', 'CREATE INDEX ix_registration_cleanup ON kalillac.account_registration_intents USING btree (expires_at, id)', False, True, True, True], ['account_registration_intents', 'ix_registration_lookup', 'CREATE INDEX ix_registration_lookup ON kalillac.account_registration_intents USING btree (address_lookup_key_version, address_lookup_digest, generation)', False, True, True, True], ['account_registration_intents', 'pk_account_registration_intents', 'CREATE UNIQUE INDEX pk_account_registration_intents ON kalillac.account_registration_intents USING btree (id)', True, True, True, True], ['account_registration_intents', 'uq_account_registration_intents_address_id', 'CREATE UNIQUE INDEX uq_account_registration_intents_address_id ON kalillac.account_registration_intents USING btree (address_id, generation)', True, True, True, True], ['account_registration_intents', 'uq_account_registration_intents_token_digest', 'CREATE UNIQUE INDEX uq_account_registration_intents_token_digest ON kalillac.account_registration_intents USING btree (token_digest)', True, True, True, True], ['account_registration_intents', 'uq_registration_current_address', 'CREATE UNIQUE INDEX uq_registration_current_address ON kalillac.account_registration_intents USING btree (address_id) WHERE is_current', True, True, True, True], ['account_registration_intents', 'uq_registration_current_lookup', 'CREATE UNIQUE INDEX uq_registration_current_lookup ON kalillac.account_registration_intents USING btree (address_lookup_key_version, address_lookup_digest) WHERE is_current', True, True, True, True], ['users', 'uq_users_email', 'CREATE UNIQUE INDEX uq_users_email ON kalillac.users USING btree (email)', True, True, True, True], ['users', 'uq_users_registration_intent', 'CREATE UNIQUE INDEX uq_users_registration_intent ON kalillac.users USING btree (registration_intent_id)', True, True, True, True]], 'triggers': [['trg_registration_intent_lifecycle', 'CREATE TRIGGER trg_registration_intent_lifecycle BEFORE INSERT OR DELETE OR UPDATE ON kalillac.account_registration_intents FOR EACH ROW EXECUTE FUNCTION kalillac.registration_intent_lifecycle_guard()'], ['trg_users_registration_identity', 'CREATE TRIGGER trg_users_registration_identity BEFORE UPDATE ON kalillac.users FOR EACH ROW EXECUTE FUNCTION kalillac.registration_user_identity_guard()'], ['trg_users_registration_provenance', 'CREATE TRIGGER trg_users_registration_provenance BEFORE INSERT ON kalillac.users FOR EACH ROW EXECUTE FUNCTION kalillac.registration_user_insert_guard()']]}

def validate_security_definitions(session,role,runtime,owner):
    """Compare trusted definitions, without executing untrusted stored guards."""
    import re,json
    from sqlalchemy import text
    from kalillac_db.migration_roles import validate_role_name
    from .registration_crypto import RegistrationUnavailable
    def require(value):
        if not value:raise RegistrationUnavailable()
    session.execute(text("SET LOCAL search_path=pg_catalog,pg_temp"))
    functions=session.execute(text("""SELECT p.oid,p.proname,p.prosrc,
        pg_get_userbyid(p.proowner),p.proconfig,p.prosecdef,p.provolatile,
        p.proparallel,p.proleakproof,p.proisstrict,p.prokind,l.lanname,
        pg_get_function_identity_arguments(p.oid),pg_get_function_result(p.oid),
        p.procost,p.prosupport::oid,p.probin
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname='kalillac' AND p.proname=ANY(:names)"""),
        {"names":list(guard_functions(role,"unused"))}).all()
    by={r[1]:r for r in functions}
    require(len(functions)==len(by)==4)
    match=re.search(r"session_user='([a-z_][a-z0-9_]*)' AND EXISTS",by["registration_intent_lifecycle_guard"][2])
    require(match is not None)
    migrator=validate_role_name(match.group(1),"registration migration role")
    require(len({migrator,role,runtime,owner})==4)
    require(session.execute(text("SELECT pg_has_role(:m,:o,'MEMBER')"),{"m":migrator,"o":owner}).scalar_one())
    canonical=guard_functions(role,migrator)
    for name,definition in canonical.items():
        row=by[name]
        # The identity guard may be 0011's v1 or 0014's v2; v1_schema pairs it with its revision.
        bodies={definition.split("$guard$")[1]}
        if name=="registration_user_identity_guard":bodies.add(identity_guard_v2(role).split("$guard$")[1])
        require(row[2] in bodies)
        require(tuple(row[3:12])==(owner,["search_path=pg_catalog, pg_temp"],True,"v","u",False,False,"f","plpgsql"))
        require(tuple(row[12:])==(("exclusive boolean","void",100.0,0,None) if name=="registration_control_lock" else ("","trigger",100.0,0,None)))
        acl=session.execute(text("""SELECT pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable,
            pg_get_userbyid(a.grantor) FROM pg_proc p CROSS JOIN LATERAL
            aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a WHERE p.oid=:oid"""),{"oid":row[0]}).all()
        expected={(owner,"EXECUTE",False,owner)}
        if name=="registration_control_lock":expected|={(role,"EXECUTE",False,owner),(runtime,"EXECUTE",False,owner)}
        require(set(map(tuple,acl))==expected)
    constraints=session.execute(text("""SELECT t.relname,c.conname,pg_get_constraintdef(c.oid,false),
        c.convalidated,c.condeferrable,c.condeferred,c.connoinherit
        FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname='kalillac' AND (t.relname IN ('account_registration_intents','account_registration_activation')
        OR c.conname='fk_users_registration_intent') ORDER BY 1,2""")).all()
    require([list(r) for r in constraints]==CANONICAL_CATALOG["constraints"])
    indexes=session.execute(text("""SELECT t.relname,i.relname,pg_get_indexdef(i.oid),x.indisunique,
        x.indisvalid,x.indisready,x.indimmediate
        FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid JOIN pg_class t ON t.oid=x.indrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='kalillac'
        AND (t.relname IN ('account_registration_intents','account_registration_activation')
        OR i.relname IN ('uq_users_registration_intent','uq_users_email')) ORDER BY 1,2""")).all()
    require([list(r) for r in indexes]==CANONICAL_CATALOG["indexes"])
    triggers=session.execute(text("""SELECT t.tgname,pg_get_triggerdef(t.oid,false),
        t.tgfoid,t.tgenabled,t.tgqual IS NULL,t.tgdeferrable,t.tginitdeferred
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='kalillac' AND NOT t.tgisinternal AND t.tgname=ANY(:names) ORDER BY 1"""),
        {"names":[r[0] for r in CANONICAL_CATALOG["triggers"]]}).all()
    require([list(r[:2]) for r in triggers]==CANONICAL_CATALOG["triggers"])
    binding={"trg_registration_intent_lifecycle":"registration_intent_lifecycle_guard",
        "trg_users_registration_identity":"registration_user_identity_guard",
        "trg_users_registration_provenance":"registration_user_insert_guard"}
    for row in triggers:require(tuple(row[2:])==(by[binding[row[0]]][0],"A",True,False,False))
    owners=session.execute(text("""SELECT pg_get_userbyid(c.relowner) FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='kalillac'
        AND c.relname IN ('users','account_registration_intents','account_registration_activation')""")).scalars().all()
    require(len(owners)==3 and set(owners)=={owner})
    for principal in (role,runtime):
        require(session.execute(text("SELECT has_schema_privilege(:r,'kalillac','USAGE') AND NOT has_schema_privilege(:r,'kalillac','USAGE WITH GRANT OPTION')"),{"r":principal}).scalar_one())
        require(session.execute(text("SELECT has_database_privilege(:r,current_database(),'CONNECT') AND NOT has_database_privilege(:r,current_database(),'CONNECT WITH GRANT OPTION') AND NOT has_database_privilege(:r,current_database(),'CREATE')"),{"r":principal}).scalar_one())
    receipt=session.execute(text("SELECT obj_description('kalillac.account_registration_activation'::regclass,'pg_class')")).scalar_one()
    try:receipt=json.loads(receipt)
    except (ValueError,TypeError):raise RegistrationUnavailable() from None
    require(isinstance(receipt,dict) and set(receipt)=={"format_version","registration_role","schema_usage_added","database_connect_added"})
    require(type(receipt["format_version"]) is int and receipt["format_version"]==1 and receipt["registration_role"]==role)
    require(type(receipt["schema_usage_added"]) is bool and type(receipt["database_connect_added"]) is bool)
