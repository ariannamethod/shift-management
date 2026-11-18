// Read-only chat regression against the demo; never confirms a shift.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = require("node:fs");
(async () => {
    const credentials = JSON.parse(
        fs.readFileSync(process.env.DEMO_CREDENTIALS, "utf8"),
    );
    const browser = await chromium.launch({
        headless: true,
        args: ["--no-sandbox"],
    });
    try {
        for (const role of ["manager", "worker"]) {
            const context = await browser.newContext();
            const page = await context.newPage();
            const errors = [];
            page.on("pageerror", (e) => errors.push(e.message));
            await page.goto(credentials.url);
            await page
                .locator("#login-form input[name=login]")
                .fill(credentials[role].login);
            await page
                .locator("#login-form input[name=password]")
                .fill(credentials[role].password);
            await page.locator("#login-form button").click();
            await page.locator("#calendar table").waitFor();
            if (role === "worker") {
                const response = page.waitForResponse(
                    (r) =>
                        r.url().includes("/api/week?start=") &&
                        r.status() === 200,
                );
                await page.locator("#next-week").click();
                await response;
            }
            await page.locator("#itzik-toggle").click();
            async function say(text) {
                await page.locator("#itzik-text").fill(text);
                await page.locator("#itzik-form button").click();
            }
            await say("קבע לי ליום ראשון בבוקר");
            if (role === "manager") {
                await page
                    .locator("#itzik-messages")
                    .getByText("כרגע מחובר חשבון מנהל", { exact: false })
                    .waitFor();
                if (await page.locator("#itzik-messages button").count())
                    throw Error("Manager received booking confirmation");
            } else {
                await page
                    .locator("#itzik-messages")
                    .getByText("באילו שעות —", { exact: false })
                    .waitFor();
                await say("מ 9 עד 13");
                await page
                    .locator("#itzik-messages")
                    .getByRole("button", { name: "אישור הרשמה", exact: true })
                    .waitFor();
                await page.screenshot({
                    path: "/tmp/shift-itzik-hour-followup.png",
                    fullPage: true,
                });
            }
            if (errors.length) throw Error(errors.join("\n"));
            console.log(
                role +
                    ": screenshot phrase handled in chat" +
                    (role === "worker"
                        ? ", hour-only follow-up offered confirmation."
                        : ", role explained."),
            );
            await page.locator("#logout").click();
            await page.locator("#login-screen").waitFor({ state: "visible" });
            await context.close();
        }
    } finally {
        await browser.close();
    }
})().catch((e) => {
    console.error(e);
    process.exit(1);
});
