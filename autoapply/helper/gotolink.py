from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.firefox import GeckoDriverManager
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support import expected_conditions as EC
import time
from autoapply.helper.genai import CoverLetterGenerator
import os
from dotenv import load_dotenv
from autoapply.helper.excel_handler import ExcelHandler

load_dotenv()  # Load variables from .env file

class Gotolink:
    def __init__(self):
        self.options = FirefoxOptions()
        profile_path = os.getenv("PROFILE_PATH")
        self.options.add_argument("-profile")
        self.options.add_argument(profile_path)
        self.driver = webdriver.Firefox(service=FirefoxService(executable_path=GeckoDriverManager().install()), options=self.options)
        self.excel_handler = ExcelHandler()
        self.cover_letter_generator = CoverLetterGenerator()
        self.auto_submit = os.getenv("AUTO_SUBMIT", "false").strip().lower() == "true"

    def process_link(self, link):
        original_window = self.driver.current_window_handle
        try:
            self.driver.execute_script("window.open('about:blank');")
            self.driver.switch_to.window(self.driver.window_handles[-1])
            self.driver.get(link)
            text = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CLASS_NAME, "styles_description__uLHQ_"))
            ).text
            cover_letter = self.cover_letter_generator.generate_cover_letter(job_description=text)
            WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.CLASS_NAME, "styles_applyButton__k2sNa"))
            ).click()
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CLASS_NAME, "styles_component__kpbt6"))
            ).send_keys(cover_letter)

            status = "Drafted (not submitted)"
            if self.auto_submit:
                WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-test='JobApplicationModal--SubmitButton']"))
                ).click()
                status = "Submitted"

            self.excel_handler.add_application(link, text, cover_letter, status)
            self.excel_handler.save_excel()
            print(f"{status}: {link}")
            time.sleep(2)
        except Exception as error:
            print(f"Failed to process {link}: {error}")
        finally:
            try:
                if self.driver.current_window_handle != original_window:
                    self.driver.close()
                    self.driver.switch_to.window(original_window)
            except Exception:
                pass

    def process_links(self, d):
        self.driver.get('https://wellfound.com/jobs')
        selected_links = [job["link"] for job in d if job.get("checked") == "1"]
        if not selected_links:
            print("No jobs were selected.")
            self.driver.quit()
            return
        if not self.auto_submit:
            print("Draft mode is enabled. Set AUTO_SUBMIT=true in .env to submit applications.")
        try:
            for link in selected_links:
                self.process_link(link)
        finally:
            self.driver.quit()
