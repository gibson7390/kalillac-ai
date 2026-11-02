"""Functional V2 browser states using the existing isolated, loopback-only CDP harness.

Build with VITE_ACCOUNT_REGISTRATION_V2_ENABLED=true into a separate directory;
KALILLAC_REGISTRATION_UI_DIST selects that test artifact, never production config.
"""
from __future__ import annotations
import json,os
from pathlib import Path
import pytest
from test_account_ui import AccountSite,AccountPage,FakeAccountApi,Raw,TOKEN,OTHER_TOKEN,PASSWORD,FETCH_RECORDER,DESKTOP
from registration_ui_contract_support import install_contract,CONTRACT_PATH
from test_homepage_handoff import browser_page,require_browser_and_build,serve_file

class V2Site(AccountSite):
    def __init__(self,dist):self.dist=dist;super().__init__()
    def route(self,raw):
        path=raw.split("?",1)[0]
        if path=="/account" or path.startswith("/account/"):
            return 200,"text/html; charset=utf-8",(self.dist/"account.html").read_bytes()
        if path.startswith("/app/"):return super().route(raw)
        return serve_file(self.dist,path.lstrip("/"))

@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    browser=require_browser_and_build()
    raw=os.getenv("KALILLAC_REGISTRATION_UI_DIST")
    assert raw,"explicit V2 test build required"
    dist=Path(raw).resolve()
    assert (dist/"account.html").is_file(),"V2 build missing"
    profile=tmp_path_factory.mktemp("registration-browser")
    site=V2Site(dist)
    with site:
        with browser_page(browser,profile,DESKTOP,False) as (dt,session):
            dt.call("Runtime.enable",{},session);dt.call("Log.enable",{},session)
            dt.call("Page.addScriptToEvaluateOnNewDocument",{"source":FETCH_RECORDER},session)
            yield AccountPage(dt,session,site)

@pytest.fixture
def page(browser,monkeypatch):
    install_contract(monkeypatch,FakeAccountApi,"email-first-v1")
    browser.dt.call("Page.navigate",{"url":"about:blank"},browser.session)
    browser.wait_for("location.href===\'about:blank\'")
    browser.site.api.reset()
    browser.site.api.registration_contract="email-first-v1"
    browser.resize(DESKTOP,False)
    yield browser
    assert browser.eval("return localStorage.length+sessionStorage.length;")==0
    assert browser.eval("return document.cookie;")==""
    for gate_list in browser.site.api.holds.values():
        for gate in gate_list:gate.release()

def submit(page):
    page.click_selector("button[type=submit]")

def passwords(page):
    page.type_into("#new-password",PASSWORD);page.type_into("#confirm-password",PASSWORD)

def test_email_only_neutral_loading_success_and_resend(page):
    path="/api/account/register"
    page.site.api.override("POST",path,202,{"ok":True})
    page.site.api.override("POST",path,202,{"ok":True})
    page.open("/account/create",ready="document.querySelector('#email')")
    assert page.eval("return document.querySelectorAll('input[type=password]').length;")==0
    page.type_into("#email","synthetic@example.test");submit(page)
    page.wait_for("document.querySelector('[data-testid=registration-check-email]')")
    assert "Check your email" in page.text()
    assert "already uses" not in page.text()
    calls=[c for c in page.fetches() if c["url"]==path]
    assert len(calls)==1 and calls[0]["keys"]==["email"]
    page.click_selector("main button")
    page.wait_for("window.__fetches.filter(c=>c.url==='/api/account/register' && c.method==='POST').length===2 && !document.querySelector('main button').disabled")
    assert len([c for c in page.fetches() if c["url"]==path])==2

def test_client_validation_focus_and_keyboard(page):
    page.open("/account/create",ready="document.querySelector('#email')")
    submit(page)
    assert page.active()=="email"
    assert page.eval("return document.querySelector('#email').getAttribute('aria-invalid');")=="true"
    assert not mutations(page)
    page.type_into("#email","synthetic@example.test")
    page.key("Tab","Tab",9)
    assert page.eval("return document.activeElement.tagName;")=="BUTTON"

def test_fragment_scrubbed_password_only_and_no_automatic_effect(page):
    page.open("/account/create/confirm#token="+TOKEN,ready="document.querySelector('#new-password')")
    assert page.eval("return location.hash;")==""
    assert not mutations(page)
    assert page.eval("return document.querySelectorAll('#email').length;")==0
    safe=TOKEN not in page.eval("return document.documentElement.outerHTML+document.title;")
    assert safe
    assert page.eval("return document.querySelector('#new-password').autocomplete;")=="new-password"

@pytest.mark.parametrize("code",["registration_pending","registration_unavailable","busy"])
def test_retryable_states_keep_capability_without_polling(page,code):
    path="/api/account/register/confirm"
    page.site.api.override("POST",path,503,{"error":code},{"Retry-After":"1"})
    page.site.api.override("POST",path,200,{"ok":True})
    page.open("/account/create/confirm#token="+TOKEN,ready="document.querySelector('#new-password')")
    passwords(page);submit(page)
    page.wait_for("document.querySelector('[data-testid=account-notice]')")
    assert len([c for c in page.fetches() if c["url"]==path])==1
    assert page.eval("return document.querySelector('#new-password')!==null;")
    submit(page)
    page.wait_for("document.querySelector('[data-testid=registration-complete]')")
    assert "not been signed in" in page.text()
    assert len([c for c in page.fetches() if c["url"]==path])==2
    assert not any(c["url"].endswith("/login") for c in page.fetches())

@pytest.mark.parametrize("state",["expired","consumed","superseded"])
def test_terminal_links_use_the_uniform_invalid_state(page,state):
    page.site.api.override("POST","/api/account/register/confirm",400,{"error":"invalid_or_expired_token"})
    page.open("/account/create/confirm#token="+TOKEN,ready="document.querySelector('#new-password')")
    passwords(page);submit(page)
    page.wait_for("document.querySelector('[data-testid=registration-invalid]')")
    assert "invalid, expired, replaced or already used" in page.text()
    assert not page.eval("return document.querySelector('input[type=password]');")

def test_stale_response_does_not_replace_new_credential(page):
    gate=page.site.api.hold("POST","/api/account/register/confirm",(400,{"error":"invalid_or_expired_token"},{}))
    page.open("/account/create/confirm#token="+TOKEN,ready="document.querySelector('#new-password')")
    passwords(page);submit(page)
    assert gate.arrived.wait(5)
    page.dt.call("Page.navigate",{"url":page.site.origin+"/account/create/confirm#token="+OTHER_TOKEN},page.session)
    page.wait_for("location.hash==='' && document.querySelector('#new-password').value===''")
    gate.release()
    page.wait_for("window.__jsonDone>=1")
    assert page.eval("return document.querySelector('#new-password')!==null;")
    assert not page.eval("return document.querySelector('[data-testid=registration-invalid]');")
    safe=OTHER_TOKEN not in page.eval("return document.documentElement.outerHTML;")
    assert safe

@pytest.mark.parametrize("size,mobile",[((1440,900),False),((390,844),True),((320,640),True)])
def test_responsive_confirmation_has_labels_and_no_overflow(page,size,mobile):
    page.resize(size,mobile)
    page.open("/account/create/confirm#token="+TOKEN,ready="document.querySelector('#new-password')")
    assert page.eval("return document.documentElement.scrollWidth<=innerWidth;")
    assert page.eval("return [...document.querySelectorAll('main label')].every(e=>!!document.getElementById(e.htmlFor));")
    page.click_selector("button[type=submit]")
    assert page.active()=="new-password"

@pytest.mark.parametrize("fragment",["","#token=bad","#other=missing"])
def test_missing_or_malformed_fragment_never_fetches(page,fragment):
    page.open("/account/create/confirm"+fragment,ready="document.querySelector('[data-testid=registration-invalid]')")
    assert page.eval("return location.hash;")==""
    assert not mutations(page)

def test_email_suppression_and_rate_limit_stay_neutral(page):
    path="/api/account/register"
    page.site.api.override("POST",path,202,{"ok":True})
    page.site.api.override("POST",path,429,{"error":"too_many_attempts"},{"Retry-After":"900"})
    page.open("/account/create",ready="document.querySelector('#email')")
    page.type_into("#email","synthetic@example.test");submit(page)
    page.wait_for("document.querySelector('[data-testid=registration-check-email]')")
    page.click_selector("main button")
    page.wait_for("document.querySelector('[data-testid=account-notice]')")
    assert "Too many requests" in page.text() and "Check your email" in page.text()
    assert not page.eval("return document.querySelector('input[type=password]');")

def test_confirm_astral_password_and_duplicate_submit(page):
    path="/api/account/register/confirm"
    gate=page.site.api.hold("POST",path,(200,{"ok":True},{}))
    page.open("/account/create/confirm#token="+TOKEN,ready="document.querySelector('#new-password')")
    for selector in ("#new-password","#confirm-password"):page.type_into(selector,"\U0001f512"*256)
    submit(page);assert gate.arrived.wait(5)
    submit(page)
    page.eval("document.querySelector('#confirm-password').focus();")
    page.key("Enter","Enter",13,"\r")
    gate.release()
    page.wait_for("document.querySelector('[data-testid=registration-complete]')")
    assert len([c for c in page.fetches() if c["url"]==path])==1
    assert not page.eval("return document.querySelector('input[type=password]');")
    page.click_selector("main a")
    page.wait_for("location.pathname==='/account/sign-in'")

def test_reload_drops_scrubbed_capability(page):
    page.open("/account/create/confirm#token="+TOKEN,ready="document.querySelector('#new-password')")
    page.dt.call("Page.reload",{},page.session)
    page.wait_for("document.querySelector('[data-testid=registration-invalid]')")
    assert not mutations(page)

def test_simultaneous_tabs_hold_independent_memory_only_proofs(page):
    target=page.dt.call("Target.createTarget",{"url":"about:blank"})["targetId"]
    session=page.dt.call("Target.attachToTarget",{"targetId":target,"flatten":True})["sessionId"]
    other=AccountPage(page.dt,session,page.site)
    try:
        for domain in ("Page","Network","Runtime","Log"):page.dt.call(domain+".enable",{},session)
        page.dt.call("Page.addScriptToEvaluateOnNewDocument",{"source":FETCH_RECORDER},session)
        page.open("/account/create/confirm#token="+TOKEN,ready="document.querySelector('#new-password')")
        other.open("/account/create/confirm#token="+OTHER_TOKEN,ready="document.querySelector('#new-password')")
        assert page.eval("return location.hash;")==other.eval("return location.hash;")==""
        assert not mutations(page) and not mutations(other)
        assert other.eval("return localStorage.length+sessionStorage.length;")==0
    finally:page.dt.call("Target.closeTarget",{"targetId":target})

def mutations(page):
    return [call for call in page.fetches() if call["method"] != "GET"]

@pytest.mark.parametrize("contract,passwords_expected",[("legacy-v1",2),("email-first-v1",0)])
def test_backend_contract_overrides_frontend_build_mode(page,contract,passwords_expected):
    page.site.api.registration_contract=contract
    page.open("/account/create",ready="document.querySelector('#email')")
    assert page.eval("return document.querySelectorAll('input[type=password]').length;")==passwords_expected
    path="/api/account/register"
    page.site.api.override("POST",path,201 if contract=="legacy-v1" else 202,
        page.site.api.user() if contract=="legacy-v1" else {"ok":True})
    page.type_into("#email","synthetic@example.test")
    if passwords_expected:
        for selector in ("#new-password","#confirm-password"):page.type_into(selector,PASSWORD)
    submit(page)
    page.wait_for("document.querySelector('[data-testid=account-created], [data-testid=registration-check-email]')")
    calls=[c for c in page.fetches() if c["url"]==path]
    assert len(calls)==1 and calls[0]["keys"]==(["email","password"] if passwords_expected else ["email"])

@pytest.mark.parametrize("status,body,headers",[
    (503,{"error":"unavailable"},{"Cache-Control":"no-store"}),
    (200,{"contract":"future-v9","nonce":"$request_nonce"},{"Cache-Control":"no-store"}),
    (200,{"contract":"legacy-v1","nonce":"0"*32},{"Cache-Control":"no-store"}),
    (200,[],{"Cache-Control":"no-store"}),
    (200,{"contract":"email-first-v1"},{"Cache-Control":"no-store"}),
    (200,{"contract":"email-first-v1","nonce":"$request_nonce","extra":True},{"Cache-Control":"no-store"}),
    (200,Raw("application/json",b"{"),{"Cache-Control":"no-store"}),
    (200,Raw("text/plain",b"{}"),{"Cache-Control":"no-store"}),
])
def test_unavailable_malformed_unsupported_stale_contract_fails_closed(page,status,body,headers):
    page.site.api.override("GET",CONTRACT_PATH,status,body,headers)
    page.open("/account/create",ready="document.querySelector('[data-testid=registration-unavailable]')")
    assert page.eval("return document.querySelector('[role=alert]')!==null;")
    assert not page.eval("return document.querySelector('form');")
    assert not mutations(page)
    page.click_selector("main button")
    page.wait_for("document.querySelector('#email')")

@pytest.mark.parametrize("before,after",[("legacy-v1","email-first-v1"),("email-first-v1","legacy-v1")])
def test_changed_mode_before_submit_clears_form_without_post(page,before,after):
    page.site.api.registration_contract=before
    page.open("/account/create",ready="document.querySelector('#email')")
    page.type_into("#email","synthetic@example.test")
    if before=="legacy-v1":
        for selector in ("#new-password","#confirm-password"):page.type_into(selector,PASSWORD)
    page.site.api.registration_contract=after
    submit(page)
    page.wait_for("document.querySelector('[data-testid=registration-unavailable]')")
    assert not page.eval("return document.querySelector('form');")
    assert not mutations(page)
    page.click_selector("main button")
    page.wait_for("document.querySelector('#email')")
    assert page.eval("return document.querySelectorAll('input[type=password]').length;")==(2 if after=="legacy-v1" else 0)

def test_legacy_backend_cannot_accept_v2_confirmation(page):
    page.site.api.registration_contract="legacy-v1"
    page.open("/account/create/confirm#token="+TOKEN,ready="document.querySelector('[data-testid=registration-unavailable]')")
    assert page.eval("return location.hash;")==""
    assert not mutations(page)

def test_late_contract_response_cannot_restore_old_route(page):
    gate=page.site.api.hold("GET",CONTRACT_PATH)
    page.open("/account/create",ready="document.querySelector('[role=status]')")
    assert gate.arrived.wait(5)
    page.open("/account/sign-in",ready="document.querySelector('#email')")
    gate.release()
    page.wait_for("location.pathname==='/account/sign-in'")
    assert not mutations(page)

def test_previous_valid_contract_response_is_rejected_after_navigation(page):
    page.open("/account/create",ready="document.querySelector('#email')")
    previous=page.site.api.last_contract_nonce
    assert isinstance(previous,str) and len(previous)==32
    page.site.api.override("GET",CONTRACT_PATH,200,
        {"contract":"email-first-v1","nonce":previous},{"Cache-Control":"no-store"})
    page.open("/account/sign-in",ready="document.querySelector('#email')")
    page.open("/account/create",ready="document.querySelector('[data-testid=registration-unavailable]')")
    assert page.site.api.last_contract_nonce!=previous
    assert not mutations(page)

def test_changed_contract_before_confirmation_clears_password_without_post(page):
    page.open("/account/create/confirm#token="+TOKEN,ready="document.querySelector('#new-password')")
    passwords(page)
    page.site.api.registration_contract="legacy-v1"
    submit(page)
    page.wait_for("document.querySelector('[data-testid=registration-unavailable]')")
    assert not page.eval("return document.querySelector('input[type=password]');")
    assert not mutations(page)

@pytest.mark.parametrize("address",["person@x.c","\u043f\u043e\u0447\u0442\u0430@example.com","a()@example.com","a"*65+"@example.com","a@"+"b"*64+".com","a@example.12"])
def test_v2_client_rejects_gateway_unsupported_addresses_without_post(page,address):
    page.open("/account/create",ready="document.querySelector('#email')")
    page.type_into("#email",address);submit(page)
    page.wait_for("document.querySelector('#email').getAttribute('aria-invalid')==='true'")
    assert page.active()=="email" and not mutations(page)

@pytest.mark.parametrize("address",["A_b.c%+-@EXAMPLE.COM","a"*64+"@example.com","a@"+"b"*63+".com","a@example."+"c"*63])
def test_v2_client_gateway_boundaries_preserve_normalized_email_body(page,address):
    path="/api/account/register"
    page.site.api.override("POST",path,202,{"ok":True})
    page.open("/account/create",ready="document.querySelector('#email')")
    page.type_into("#email",address);submit(page)
    page.wait_for("document.querySelector('[data-testid=registration-check-email]')")
    [call]=[c for c in page.site.api.calls if c["method"]=="POST" and c["path"]==path]
    assert call["body"]=={"email":address.strip().lower()}
