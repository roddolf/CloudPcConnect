#include <assert.h>
#include <string.h>

#include "rdpewa_fido.c"

static size_t assertion_count;
static const char* names[2];
static const char* display_names[2];
static size_t choice;
static BOOL accept;
static unsigned int calls;

size_t fido_assert_count(const fido_assert_t* assertion)
{
	(void)assertion;
	return assertion_count;
}

const char* fido_assert_user_name(const fido_assert_t* assertion, size_t index)
{
	(void)assertion;
	return names[index];
}

const char* fido_assert_user_display_name(const fido_assert_t* assertion, size_t index)
{
	(void)assertion;
	return display_names[index];
}

static BOOL choose(freerdp* instance, const char* const* labels, size_t count, size_t* selected)
{
	(void)instance;
	calls++;
	assert(count == 2);
	assert(strcmp(labels[0], "Example User - user@example.test") == 0);
	assert(strcmp(labels[1], "Account 2 (name unavailable)") == 0);
	*selected = choice;
	return accept;
}


/* Stubs for the PIN/run loop, reports and error responses. */
static int script_results[8];
static size_t script_count;
static size_t script_index;
static char worker_pins[8][16];
static BOOL has_pin = TRUE;
static BOOL has_uv;
static int retry_count = -1;
static unsigned int prompts;
static BOOL prompt_accept = TRUE;
static char prompt_users[8][64];
static char prompt_domains[8][16];
static HRESULT response_hr;
static BYTE response_status;
static unsigned int reports;
static char report_message[512];
static HANDLE abort_event;
static HANDLE worker_gate;
static unsigned int cancels;

bool fido_dev_has_pin(const fido_dev_t* dev)
{
	(void)dev;
	return has_pin;
}

bool fido_dev_has_uv(const fido_dev_t* dev)
{
	(void)dev;
	return has_uv;
}

int fido_dev_get_retry_count(fido_dev_t* dev, int* retries)
{
	(void)dev;
	if (retry_count < -1)
		return FIDO_ERR_INTERNAL;
	*retries = retry_count;
	return FIDO_OK;
}

int fido_dev_cancel(fido_dev_t* dev)
{
	(void)dev;
	cancels++;
	if (worker_gate)
		(void)SetEvent(worker_gate);
	return FIDO_OK;
}

HANDLE freerdp_abort_event(rdpContext* context)
{
	(void)context;
	return abort_event;
}

wStream* rdpewa_cbor_encode_webauthn_response(HRESULT hresult, BYTE ctapStatus,
                                              const BYTE* ctapData, size_t ctapLen,
                                              const RDPEWA_DEVICE_INFO* devInfo)
{
	(void)ctapData;
	(void)ctapLen;
	(void)devInfo;
	response_hr = hresult;
	response_status = ctapStatus;
	return (wStream*)&response_hr;
}

static DWORD WINAPI scripted_worker(LPVOID arg)
{
	RDPEWA_FIDO_ASYNC* a = (RDPEWA_FIDO_ASYNC*)arg;
	if (worker_gate)
		(void)WaitForSingleObject(worker_gate, INFINITE);
	assert(script_index < script_count);
	(void)_snprintf(worker_pins[script_index], sizeof(worker_pins[0]), "%s",
	                a->pin ? a->pin : "(none)");
	a->result = script_results[script_index++];
	return 0;
}

static BOOL prompt(freerdp* instance, char** username, char** password, char** domain,
                   rdp_auth_reason reason)
{
	(void)instance;
	assert(reason == AUTH_FIDO_PIN);
	assert(prompts < 8);
	(void)_snprintf(prompt_users[prompts], sizeof(prompt_users[0]), "%s",
	                *username ? *username : "(null)");
	(void)_snprintf(prompt_domains[prompts], sizeof(prompt_domains[0]), "%s",
	                *domain ? *domain : "(null)");
	prompts++;
	if (!prompt_accept)
		return FALSE;
	*password = _strdup("1234");
	return TRUE;
}

static void on_notification(void* context, const UserNotificationEventArgs* e)
{
	(void)context;
	if (e->message && strncmp(e->message, "rdpewa-error:", 13) == 0)
	{
		reports++;
		(void)_snprintf(report_message, sizeof(report_message), "%s", e->message);
	}
}

static void reset_run(const int* results, size_t count)
{
	memcpy(script_results, results, count * sizeof(*results));
	script_count = count;
	script_index = 0;
	prompts = 0;
	prompt_accept = TRUE;
	has_pin = TRUE;
	has_uv = FALSE;
	retry_count = -1;
}

static void test_run_loop_and_reports(void)
{
	freerdp instance = { 0 };
	rdpContext context = { 0 };
	RDPEWA_FIDO_ASYNC fta = { 0 };
	wEventType types[] = { DEFINE_EVENT_ENTRY(UserNotification) };
	context.instance = &instance;
	context.pubSub = PubSub_New(TRUE);
	assert(context.pubSub);
	PubSub_AddEventTypes(context.pubSub, types, ARRAYSIZE(types));
	assert(PubSub_SubscribeUserNotification(context.pubSub, on_notification) >= 0);
	abort_event = CreateEvent(nullptr, TRUE, FALSE, nullptr);
	assert(abort_event);
	instance.AuthenticateEx = prompt;

	/* First try succeeds: one prompt naming the site, no retry hint. */
	const int ok[] = { FIDO_OK };
	reset_run(ok, 1);
	assert(rdpewa_fido_run(&context, nullptr, &fta, scripted_worker, false, "login.example.test") ==
	       FIDO_OK);
	assert(prompts == 1);
	assert(strcmp(prompt_users[0], "login.example.test") == 0);
	assert(strcmp(prompt_domains[0], "(null)") == 0);
	assert(strcmp(worker_pins[0], "1234") == 0);
	assert(fta.pin == nullptr);

	/* A wrong PIN is asked for again with the retries left. */
	const int wrong_then_ok[] = { FIDO_ERR_PIN_INVALID, FIDO_OK };
	reset_run(wrong_then_ok, 2);
	retry_count = 7;
	assert(rdpewa_fido_run(&context, nullptr, &fta, scripted_worker, false, "-") == FIDO_OK);
	assert(prompts == 2);
	assert(strcmp(prompt_users[0], "(null)") == 0);
	assert(strcmp(prompt_domains[1], "7") == 0);

	/* Unknown retries say "wrong"; prompts stop after three attempts. */
	const int always_wrong[] = { FIDO_ERR_PIN_INVALID, FIDO_ERR_PIN_INVALID,
		                         FIDO_ERR_PIN_INVALID };
	reset_run(always_wrong, 3);
	retry_count = -2;
	assert(rdpewa_fido_run(&context, nullptr, &fta, scripted_worker, false, "-") ==
	       FIDO_ERR_PIN_INVALID);
	assert(prompts == RDPEWA_FIDO_PIN_ATTEMPTS);
	assert(strcmp(prompt_domains[1], "wrong") == 0 && strcmp(prompt_domains[2], "wrong") == 0);

	/* No retries left means the PIN is blocked; no further prompt. */
	reset_run(always_wrong, 1);
	retry_count = 0;
	assert(rdpewa_fido_run(&context, nullptr, &fta, scripted_worker, false, "-") ==
	       FIDO_ERR_PIN_BLOCKED);
	assert(prompts == 1);

	/* Dismissing the prompt cancels without touching the key. */
	reset_run(ok, 1);
	prompt_accept = FALSE;
	assert(rdpewa_fido_run(&context, nullptr, &fta, scripted_worker, false, "-") ==
	       FIDO_ERR_KEEPALIVE_CANCEL);
	assert(script_index == 0);

	/* Built-in verification failing falls back to the PIN. */
	const int uv_then_ok[] = { FIDO_ERR_UV_INVALID, FIDO_OK };
	reset_run(uv_then_ok, 2);
	has_uv = TRUE;
	assert(rdpewa_fido_run(&context, nullptr, &fta, scripted_worker, true, "-") == FIDO_OK);
	assert(prompts == 1);
	assert(strcmp(worker_pins[0], "(none)") == 0 && strcmp(worker_pins[1], "1234") == 0);

	/* Other failures are returned as they are. */
	const int missing[] = { FIDO_ERR_NO_CREDENTIALS };
	reset_run(missing, 1);
	assert(rdpewa_fido_run(&context, nullptr, &fta, scripted_worker, false, "-") ==
	       FIDO_ERR_NO_CREDENTIALS);
	assert(prompts == 1);

	/* A closing session cancels the key and ends the request without an answer. */
	worker_gate = CreateEvent(nullptr, TRUE, FALSE, nullptr);
	assert(worker_gate);
	(void)SetEvent(abort_event);
	const int slow[] = { FIDO_ERR_KEEPALIVE_CANCEL };
	reset_run(slow, 1);
	has_pin = FALSE;
	cancels = 0;
	assert(rdpewa_fido_run(&context, nullptr, &fta, scripted_worker, false, "-") ==
	       RDPEWA_FIDO_ABORTED);
	assert(cancels == 1 && prompts == 0);
	(void)ResetEvent(abort_event);
	(void)CloseHandle(worker_gate);
	worker_gate = nullptr;

	/* Reports name the code and site; cancels and aborts stay silent. */
	reports = 0;
	rdpewa_fido_report(&context, FIDO_ERR_NO_CREDENTIALS, "login.example.test");
	assert(reports == 1);
	assert(strcmp(report_message, "rdpewa-error:46:login.example.test:This security key has no "
	                              "passkey for this site.") == 0);
	rdpewa_fido_report(&context, FIDO_OK, "-");
	rdpewa_fido_report(&context, FIDO_ERR_KEEPALIVE_CANCEL, "-");
	rdpewa_fido_report(&context, RDPEWA_FIDO_ABORTED, "-");
	assert(reports == 1);

	PubSub_Free(context.pubSub);
	(void)CloseHandle(abort_event);
}

static void test_site_and_responses(void)
{
	char site[RDPEWA_FIDO_SITE_MAX + 1];
	char longest[RDPEWA_FIDO_SITE_MAX + 2];

	rdpewa_fido_site(site, sizeof(site), "login.microsoft.com");
	assert(strcmp(site, "login.microsoft.com") == 0);
	rdpewa_fido_site(site, sizeof(site), nullptr);
	assert(strcmp(site, "-") == 0);
	rdpewa_fido_site(site, sizeof(site), "");
	assert(strcmp(site, "-") == 0);
	rdpewa_fido_site(site, sizeof(site), "evil site:1\n");
	assert(strcmp(site, "-") == 0);
	memset(longest, 'a', sizeof(longest) - 1);
	longest[sizeof(longest) - 1] = '\0';
	rdpewa_fido_site(site, sizeof(site), longest);
	assert(strcmp(site, "-") == 0);
	longest[RDPEWA_FIDO_SITE_MAX] = '\0';
	rdpewa_fido_site(site, sizeof(site), longest);
	assert(strlen(site) == RDPEWA_FIDO_SITE_MAX);

	assert(rdpewa_fido_error_response(FIDO_ERR_NO_CREDENTIALS, nullptr));
	assert(response_hr == S_OK && response_status == 0x2E);
	assert(rdpewa_fido_error_response(FIDO_ERR_KEEPALIVE_CANCEL, nullptr));
	assert(response_hr == S_OK && response_status == 0x2D);
	assert(rdpewa_fido_error_response(FIDO_ERR_INTERNAL, nullptr));
	assert(response_hr == E_FAIL && response_status == 0x01);
}

int main(void)
{
	freerdp instance = { 0 };
	rdpContext context = { 0 };
	const fido_assert_t* assertion = (const fido_assert_t*)&assertion_count;
	size_t selected = 99;
	context.instance = &instance;
	instance.ChooseFidoCredential = choose;

	assertion_count = 0;
	assert(!rdpewa_fido_choose_assertion(&context, assertion, &selected));
	assertion_count = 1;
	assert(rdpewa_fido_choose_assertion(&context, assertion, &selected));
	assert(selected == 0 && calls == 0);
	assertion_count = 2;
	names[0] = "user@example.test";
	display_names[0] = "Example User";
	choice = 1;
	accept = TRUE;
	assert(rdpewa_fido_choose_assertion(&context, assertion, &selected));
	assert(selected == 1 && calls == 1);
	accept = FALSE;
	assert(!rdpewa_fido_choose_assertion(&context, assertion, &selected));
	accept = TRUE;
	choice = 2;
	assert(!rdpewa_fido_choose_assertion(&context, assertion, &selected));
	instance.ChooseFidoCredential = NULL;
	assert(!rdpewa_fido_choose_assertion(&context, assertion, &selected));
	assertion_count = 129;
	assert(!rdpewa_fido_choose_assertion(&context, assertion, &selected));

	test_run_loop_and_reports();
	test_site_and_responses();
	return 0;
}
