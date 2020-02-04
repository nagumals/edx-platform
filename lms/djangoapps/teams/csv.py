"""
CSV processing and generation utilities for Teams LMS app.
"""

import csv
from django.contrib.auth.models import User
from student.models import CourseEnrollment

from lms.djangoapps.teams.models import CourseTeam, CourseTeamMembership
from .utils import emit_team_event


def load_team_membership_csv(course, response):
    """
    Load a CSV detailing course membership.

    Arguments:
        course (CourseDescriptor): Course module for which CSV
            download has been requested.
        response (HttpResponse): Django response object to which
            the CSV content will be written.
    """
    # This function needs to be implemented (TODO MST-31).
    _ = course
    not_implemented_message = (
        "Team membership CSV download is not yet implemented."
    )
    response.write(not_implemented_message + "\n")


class TeamMembershipImportManager(object):
    """
    A manager class that is responsible the import process of csv file including validation and creation of
    team_courseteam and teams_courseteammembership objects.
    """

    def __init__(self, course):
        self.validation_errors = []
        self.teamset_ids = []
        self.user_ids_by_teamset_id = {}
        self.teamset_ids = []
        self.number_of_record_added = 0
        self.course = course
        self.max_errors = 0
        self.existing_course_team_memberships = {}
        self.existing_course_teams = {}

    @property
    def import_succeeded(self):
        """
        Helper wrapper that tells us the status of the import
        """
        return not self.validation_errors

    def set_team_membership_from_csv(self, input_file):
        """
        Assigns team membership based on the content of an uploaded CSV file.
        Returns true if there were no issues.
        """
        reader = csv.DictReader((line.decode('utf-8-sig').strip() for line in input_file.readlines()))
        self.teamset_ids = reader.fieldnames[2:]
        row_dictionaries = []
        csv_usernames = set()
        if not self.validate_teamsets():
            return False
        self.load_user_ids_by_teamset_id()
        self.load_course_team_memberships()
        self.load_course_teams()
        # process student rows:
        for row in reader:
            username = row['user']
            if not username:
                continue
            if not self.is_username_unique(username, csv_usernames):
                return False
            csv_usernames.add(username)
            user = self.get_user(username)
            if user is None:
                continue
            if not self.validate_user_enrolled_in_course(user):
                row['user'] = None
                continue
            row['user'] = user

            if not self.validate_user_assignment_to_team_and_teamset(row):
                return False
            row_dictionaries.append(row)

        if not self.validation_errors:
            for row in row_dictionaries:
                self.add_user_to_team(row)
            return True
        else:
            return False

    def load_course_team_memberships(self):
        """
        Caches existing team memberships by (user_id, teamset_id)
        """
        for membership in CourseTeamMembership.get_memberships(course_ids=[self.course.id]):
            user_id = membership.user_id
            teamset_id = membership.team.topic_id
            self.existing_course_team_memberships[(user_id, teamset_id)] = membership.team.id

    def load_course_teams(self):
        """
        Caches existing course teams by (team_name, topic_id)
        """
        for team in CourseTeam.objects.filter(course_id=self.course.id):
            self.existing_course_teams[(team.name, team.topic_id)] = team

    def validate_teamsets(self):
        """
        Validates team set names. Returns true if there are no errors.
        The following conditions result in errors:
        Teamset does not exist
        Teamset id is duplicated
        Also populates the teamset_names_list.
        header_row is the list of teamset_ids
        """
        teamset_ids = {ts.teamset_id for ts in self.course.teams_configuration.teamsets}
        dupe_set = set()
        for teamset_id in self.teamset_ids:
            if teamset_id in dupe_set:
                self.validation_errors.append("Teamset with id " + teamset_id + " is duplicated.")
                return False
            dupe_set.add(teamset_id)
            if teamset_id not in teamset_ids:
                self.validation_errors.append("Teamset with id " + teamset_id + " does not exist.")
                return False
        return True

    def load_user_ids_by_teamset_id(self):
        for teamset_id in self.teamset_ids:
            self.user_ids_by_teamset_id[teamset_id] = {
                membership.user_id for membership in
                CourseTeamMembership.objects.filter(
                    team__course_id=self.course.id, team__topic_id=teamset_id
                )
            }

    def validate_user_enrolled_in_course(self, user):
        """
        Invalid states:
            user not enrolled in course
        """
        if not CourseEnrollment.is_enrolled(user, self.course.id):
            self.validation_errors.append('User ' + user.username + ' is not enrolled in this course.')
            return False

        return True

    def is_username_unique(self, username, usernames_found_so_far):
        """
        Ensures that username exists only once in an input file
        """
        if username in usernames_found_so_far:
            error_message = 'Username {} was found more than once in input file.'.format(username)
            if self.add_error_and_check_if_max_exceeded(error_message):
                return False
        return True

    def validate_user_assignment_to_team_and_teamset(self, row):
        """
        Validates a user entry relative to an existing team.
        row is a dictionary where key is column name and value is the row value
        [andrew],masters,team1,,team3
        [joe],masters,,team2,team3
        """
        user = row['user']
        for teamset_id in self.teamset_ids:
            team_name = row[teamset_id]
            if not team_name:
                continue
            try:
                # checks for a team inside a specific team set. This way team names can be duplicated across
                # teamsets
                team = self.existing_course_teams[(team_name, teamset_id)]
            except KeyError:
                # if a team doesn't exists, the validation doesn't apply to it.
                all_teamset_user_ids = self.user_ids_by_teamset_id[teamset_id]
                error_message = 'The user {0} is already a member of a team inside teamset {1} in this course.'.format(
                    user.username, teamset_id
                )
                if user.id in all_teamset_user_ids and self.add_error_and_check_if_max_exceeded(error_message):
                    return False
                else:
                    self.user_ids_by_teamset_id[teamset_id].add(user.id)
                    continue
            max_team_size = self.course.teams_configuration.default_max_team_size
            if max_team_size is not None and team.users.count() >= max_team_size:
                if self.add_error_and_check_if_max_exceeded('Team ' + team.team_id + ' is already full.'):
                    return False

            if (user.id, team.topic_id) in self.existing_course_team_memberships:
                error_message = 'The user {0} is already a member of a team inside teamset {1} in this course.'.format(
                    user.username, team.topic_id
                )
                if self.add_error_and_check_if_max_exceeded(error_message):
                    return False
        return True

    def add_error_and_check_if_max_exceeded(self, error_message):
        """
        Adds an error to the error collection.
        :param error_message:
        :return: True if maximum error threshold is exceeded and processing must stop
                 False if maximum error threshold is NOT exceeded and processing can continue
        """
        self.validation_errors.append(error_message)
        return len(self.validation_errors) >= self.max_errors

    def add_user_to_team(self, user_row):
        """
        Creates a CourseTeamMembership entry - i.e: a relationship between a user and a team.
        user_row is a dictionary where key is column name and value is the row value.
        {'mode': ' masters','topic_0': '','topic_1': 'team 2','topic_2': None,'user': <user_obj>}
         andrew,masters,team1,,team3
        joe,masters,,team2,team3
        """
        user = user_row['user']
        for teamset_id in self.teamset_ids:
            team_name = user_row[teamset_id]
            if not team_name:
                continue
            if (team_name, teamset_id) not in self.existing_course_teams:
                team = CourseTeam.create(
                    name=team_name,
                    course_id=self.course.id,
                    description='Import from csv',
                    topic_id=teamset_id
                )
                team.save()
            team.add_user(user)
            emit_team_event(
                'edx.team.learner_added',
                team.course_id,
                {
                    'team_id': team.team_id,
                    'user_id': user.id,
                    'add_method': 'team_csv_import'
                }
            )
            self.number_of_record_added += 1

    def get_user(self, user_name):
        """
        Gets the user object from user_name/email/locator
        user_name: the user_name/email/user locator
        """
        try:
            return User.objects.get(username=user_name)
        except User.DoesNotExist:
            try:
                return User.objects.get(email=user_name)
            except User.DoesNotExist:
                self.validation_errors.append('Username or email ' + user_name + ' does not exist.')
                return None
                # TODO - handle user key case
