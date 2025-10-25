# from browser_use_sdk import AsyncBrowserUse
# from browser_use import Tools
# from
from browser_use import Browser, Agent, ChatBrowserUse, Tools
from browser_use_sdk import BrowserUse
import asyncio
import os
import dotenv

dotenv.load_dotenv()

os.environ["ANONYMIZED_TELEMETRY"] = "false"

client = BrowserUse(api_key=os.getenv("BROWSER_USE_API_KEY"))

email = "4dennisk@gmail.com"

session =  client.sessions.create_session(
    profile_id="faf3ff86-030f-4b90-a5fc-97e1c08e03d0",
    proxy_country_code="us"  # or other country codes
)

async def main():

    tools = Tools()

    @tools.action(description='Ask the user to do something')
    async def ask_user():
        agent.pause()
        try:
            loop = asyncio.get_running_loop()
            # Run blocking console input off the event loop to keep browser session alive.
            data = await loop.run_in_executor(
                None,
                lambda: input("Please provide the required information and press Enter to continue...")
            )
        finally:
            agent.resume()
        return data
    


    browser = Browser(
        use_cloud=True
    )

    task=(
            f"sign up {email} to a ton of mailing lists on https://substack.com/browse/staff-picks"
        )
    
    agent = Agent(
        task=task,
        browser=browser,
        session=session,
        llm=ChatBrowserUse(),
        use_vision=True,
        tools=tools,
    )

    await agent.run()


if __name__ == "__main__":
    output = asyncio.run(main())
    print(output)


