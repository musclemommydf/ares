// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * RF Propagation Simulator — React Native Mobile App
 * Android & iOS using Expo + React Navigation
 */
import React from 'react'
import { NavigationContainer } from '@react-navigation/native'
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs'
import { StatusBar } from 'expo-status-bar'
import { SafeAreaProvider } from 'react-native-safe-area-context'
import { GestureHandlerRootView } from 'react-native-gesture-handler'
import { Ionicons } from '@expo/vector-icons'

import MapScreen from './screens/MapScreen'
import SimulatorScreen from './screens/SimulatorScreen'
import SettingsScreen from './screens/SettingsScreen'
import SpaceWeatherScreen from './screens/SpaceWeatherScreen'

const Tab = createBottomTabNavigator()

const COLORS = {
  bg: '#0d1117',
  bgSecondary: '#161b22',
  border: '#30363d',
  accent: '#00b4d8',
  green: '#06d6a0',
  textSecondary: '#8b949e',
}

export default function App() {
  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <SafeAreaProvider>
        <NavigationContainer
          theme={{
            dark: true,
            colors: {
              primary: COLORS.accent,
              background: COLORS.bg,
              card: COLORS.bgSecondary,
              text: '#e6edf3',
              border: COLORS.border,
              notification: COLORS.accent,
            },
          }}
        >
          <StatusBar style="light" backgroundColor={COLORS.bgSecondary} />
          <Tab.Navigator
            screenOptions={({ route }) => ({
              tabBarIcon: ({ focused, color, size }) => {
                const icons: Record<string, string> = {
                  Map: focused ? 'map' : 'map-outline',
                  Simulate: focused ? 'radio' : 'radio-outline',
                  'Space Wx': focused ? 'planet' : 'planet-outline',
                  Settings: focused ? 'settings' : 'settings-outline',
                }
                return (
                  <Ionicons
                    name={icons[route.name] as any}
                    size={size}
                    color={color}
                  />
                )
              },
              tabBarActiveTintColor: COLORS.accent,
              tabBarInactiveTintColor: COLORS.textSecondary,
              tabBarStyle: {
                backgroundColor: COLORS.bgSecondary,
                borderTopColor: COLORS.border,
                borderTopWidth: 1,
              },
              headerStyle: {
                backgroundColor: COLORS.bgSecondary,
                borderBottomColor: COLORS.border,
                borderBottomWidth: 1,
              },
              headerTintColor: '#e6edf3',
              headerTitleStyle: { fontWeight: '600', fontSize: 16 },
            })}
          >
            <Tab.Screen
              name="Map"
              component={MapScreen}
              options={{ title: 'Coverage Map' }}
            />
            <Tab.Screen
              name="Simulate"
              component={SimulatorScreen}
              options={{ title: 'Simulate' }}
            />
            <Tab.Screen
              name="Space Wx"
              component={SpaceWeatherScreen}
              options={{ title: 'Space Weather' }}
            />
            <Tab.Screen
              name="Settings"
              component={SettingsScreen}
              options={{ title: 'Settings' }}
            />
          </Tab.Navigator>
        </NavigationContainer>
      </SafeAreaProvider>
    </GestureHandlerRootView>
  )
}
